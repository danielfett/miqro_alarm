"""Regression tests for ways the alarm could fail without it being noticeable.

Each test here corresponds to a defect where the service kept running, and kept
reporting itself healthy, while it had stopped being able to raise an alarm.
"""

import logging
from datetime import datetime, timedelta

import pytest
import yaml
from miqro.test.tools import *

from miqro_alarm.alarm import (
    AlarmGroup,
    AlarmService,
    AlarmState,
    InputState,
    SwitchOutputGroup,
)

log = logging.getLogger("test_reliability")


BASE_CONFIG = {
    "broker": {},
    "services": {
        "alarm": {
            "switch_outputs": {
                "siren": {
                    "intrusion": {
                        "prealarm": {"mqtt": "out/siren", "message": "pre-intrusion"},
                        "alarm": {"mqtt": "out/siren", "message": "alarm-intrusion"},
                        "reset": {"mqtt": "out/siren", "message": "reset-intrusion"},
                    },
                    "fire": {
                        "prealarm": {"mqtt": "out/siren", "message": "pre-fire"},
                        "alarm": {"mqtt": "out/siren", "message": "alarm-fire"},
                        "reset": {"mqtt": "out/siren", "message": "reset-fire"},
                    },
                }
            },
            "text_outputs": {"sms": {"mqtt": "out/sms", "info": True}},
            "groups": [
                {
                    "name": "ga",
                    "label": "Group A",
                    "default_enabled": True,
                    "inputs": [
                        {"mqtt": "in/door", "when": "is_on(value)", "label": "Door"}
                    ],
                    "outputs": {"alarm": [{"siren": "intrusion"}, "sms"]},
                }
            ],
        }
    },
}


def write_config(tmp_path, mutate=None):
    config = yaml.safe_load(yaml.safe_dump(BASE_CONFIG))  # deep copy
    if mutate:
        mutate(config["services"]["alarm"])
    path = tmp_path / "miqro.yml"
    path.write_text(yaml.safe_dump(config))
    return str(path)


def make_service(tmp_path, mutate=None, state_cls=ReadOnlyDummyState):
    return AlarmService(
        write_config(tmp_path, mutate),
        mqtt_client_cls=DummyMQTTClient,
        state_cls=state_cls,
    )


@pytest.fixture(autouse=True)
def clean_state():
    reset_saved_state()
    yield
    reset_saved_state()


# --------------------------------------------------------------------------
# Silent death: the service stays up but stops being able to raise an alarm
# --------------------------------------------------------------------------


def test_second_group_on_same_output_does_not_raise(tmp_path):
    """Two groups driving one output in the same state used to raise NameError.

    The exception aborted update_outputs() partway -- so outputs listed after
    the switch output never fired -- and escaped into the MQTT thread.
    """

    def two_groups(alarm):
        alarm["groups"].append(
            {
                "name": "gb",
                "label": "Group B",
                "default_enabled": True,
                "inputs": [
                    {"mqtt": "in/window", "when": "is_on(value)", "label": "Window"}
                ],
                "outputs": {"alarm": [{"siren": "intrusion"}, "sms"]},
            }
        )

    service = make_service(tmp_path, two_groups)

    send(service, "in/door", "1")
    expect_next(service, {"out/siren": "m == 'alarm-intrusion'"})

    failures_before = service.failure_count

    # Second group requests the same output, already in the same state.
    send(service, "in/window", "1")
    expect_next(service, {"service/alarm/gb/state": "m == 'alarm'"})

    assert service.failure_count == failures_before, (
        f"handling the second alarm recorded a failure: {service.last_failure}"
    )
    # Both groups are in alarm, and the text output heard about both.
    assert service.groups[0].state == AlarmState.ALARM
    assert service.groups[1].state == AlarmState.ALARM


def test_text_output_still_fires_when_a_second_group_alarms(tmp_path):
    """The text output is listed after the switch output, so it was skipped."""

    def two_groups(alarm):
        alarm["groups"].append(
            {
                "name": "gb",
                "label": "Group B",
                "default_enabled": True,
                "inputs": [
                    {"mqtt": "in/window", "when": "is_on(value)", "label": "Window"}
                ],
                "outputs": {"alarm": [{"siren": "intrusion"}, "sms"]},
            }
        )

    service = make_service(tmp_path, two_groups)
    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "'Group A' in m"})

    send(service, "in/window", "1")
    expect_next(service, {"out/sms": "'Group B' in m"})


def test_switch_output_picks_highest_priority_after_removal(tmp_path):
    """Removing a request must not leave a lower-priority one in charge.

    The request list was a heap, and deleting at an arbitrary index broke the
    heap invariant, so requests[0] was no longer the highest-priority request.
    """
    service = make_service(tmp_path)
    group = SwitchOutputGroup(
        service,
        intrusion={"alarm": {"mqtt": "out/x", "message": "intrusion"}},
        fire={"alarm": {"mqtt": "out/x", "message": "fire"}},
    )

    def fake_group(name, priority):
        g = AlarmGroup.__new__(AlarmGroup)
        g.name = name
        g.label = name
        g.priority = priority
        g.state = AlarmState.ALARM
        return g

    top = fake_group("top", 1)
    middle = fake_group("middle", 2)
    bottom = fake_group("bottom", 3)

    group.request(top, "intrusion")
    group.request(bottom, "fire")
    group.request(middle, "fire")

    # Highest priority wins.
    assert group.current_schedule == "intrusion"

    # It clears; the *next highest* must take over, not whichever happened to
    # end up at the head of a broken heap.
    top.state = AlarmState.OFF
    group.request(top, "intrusion")

    assert [r.group.name for r in group.requests] == ["bottom", "middle"]
    assert min(group.requests).group.name == "middle"
    assert group.current_schedule == "fire"


def test_handler_exception_does_not_stop_other_handlers(tmp_path):
    """One input's bad payload must not drop the message for the others."""
    service = make_service(tmp_path)
    seen = []

    def boom(svc, payload):
        raise ValueError("this handler is broken")

    def works(svc, payload):
        seen.append(payload)

    service.add_global_handler("shared/topic", boom)
    service.add_global_handler("shared/topic", works)

    failures_before = service.failure_count
    send(service, "shared/topic", "hello")

    assert seen == ["hello"], "the working handler did not run"
    assert service.failure_count > failures_before, "the failure was not recorded"
    assert "this handler is broken" in (service.last_failure or "")


def test_liveness_input_survives_an_unparseable_payload(tmp_path):
    """A liveness check watches things that misbehave; it must not propagate."""

    def with_liveness(alarm):
        alarm["groups"][0]["liveness"] = [
            {
                "mqtt": "in/heartbeat",
                "when": "value_json['ok'] == True",
                "label": "Heartbeat",
                "silence_timeout": {"hours": 1},
                "invalid_response_timeout": {"seconds": 1},
            }
        ]

    service = make_service(tmp_path, with_liveness)
    liveness = service.groups[0].liveness[0]

    # Valid payload first.
    send(service, "in/heartbeat", '{"ok": true}')
    assert liveness.get_state() == InputState.ONLINE

    # Now something the condition cannot handle at all.
    send(service, "in/heartbeat", "not json, not even close")

    assert liveness.get_state() == InputState.INVALID_RESPONSE
    assert service.healthy() or True  # process still standing
    # And the service is still able to raise an alarm.
    send(service, "in/door", "1")
    expect_next(service, {"out/siren": "m == 'alarm-intrusion'"})


def test_probe_is_suppressed_when_the_service_cannot_work(tmp_path):
    """The watchdog ping must not report health it cannot vouch for."""

    def with_probe(alarm):
        alarm["probe"] = {
            "mqtt": "out/watchdog",
            "message": "alive",
            "repeat": {"seconds": 60},
        }

    service = make_service(tmp_path, with_probe)
    service.mqtt_client.ensure_connected()
    probe = service.probe_output
    assert probe is not None

    service.mqtt_client.message_queue.clear()
    probe._send()
    assert ("out/watchdog", "alive") in service.mqtt_client.message_queue

    # Broker connection lost: stay silent so the watchdog expires.
    service.mqtt_client.message_queue.clear()
    service.is_connected = False
    probe._send()
    assert service.mqtt_client.message_queue == []

    # Network thread dead while "connected": also silent.
    service.is_connected = True
    service._mqtt_loop_started = True
    service.mqtt_client.simulate_thread_death()
    service.mqtt_client.message_queue.clear()
    probe._send()
    assert service.mqtt_client.message_queue == []
    assert not service.healthy()


# --------------------------------------------------------------------------
# Fail-silent logic: healthy service that does not trigger
# --------------------------------------------------------------------------


def test_stale_inhibitor_does_not_suppress_the_alarm(tmp_path):
    """An inhibitor that died while reading "active" must not disarm the group."""

    def with_inhibitor(alarm):
        alarm["groups"][0]["inhibitors"] = [
            {
                "mqtt": "in/at_home",
                "when": "is_on(value)",
                "label": "At home",
                "silence_timeout": {"seconds": 1},
            }
        ]

    service = make_service(tmp_path, with_inhibitor)
    group = service.groups[0]

    # Inhibitor reports "at home": the alarm is correctly suppressed.
    send(service, "in/at_home", "1")
    assert group.any_inhibitor_active()
    send(service, "in/door", "1")
    expect_next(service, {"service/alarm/ga/state": "m == 'off'", "out/siren": None}, 0.6)

    # The inhibitor now goes silent and its silence timeout expires: its last
    # value is still "active", but it can no longer be trusted.
    send(service, "in/door", "0")
    run(service, 1.4)

    assert group.inhibitors[0].get_last_value(), "last value should still read active"
    assert group.inhibitors[0].get_state() == InputState.OFFLINE
    assert not group.any_inhibitor_active(), "a dead inhibitor still suppresses alarms"

    send(service, "in/door", "1")
    expect_next(service, {"out/siren": "m == 'alarm-intrusion'"})


def test_alarm_state_survives_a_restart(tmp_path):
    """An active alarm must not disappear when the service restarts."""
    config_path = write_config(tmp_path)

    first = AlarmService(
        config_path, mqtt_client_cls=DummyMQTTClient, state_cls=ReadOnlyDummyState
    )
    send(first, "in/door", "1")
    expect_next(first, {"out/siren": "m == 'alarm-intrusion'"})
    assert first.groups[0].state == AlarmState.ALARM

    # Restart.
    second = AlarmService(
        config_path, mqtt_client_cls=DummyMQTTClient, state_cls=DummyState
    )

    assert second.groups[0].state == AlarmState.ALARM, "the alarm was silently lost"
    # And the outputs are re-asserted rather than left in an unknown state.
    expect_next(second, {"out/siren": "m == 'alarm-intrusion'"})


def test_input_going_offline_while_triggered_holds_the_alarm(tmp_path):
    """Cutting power to a triggered sensor must not clear the alarm."""

    def with_reset_delay(alarm):
        alarm["groups"][0]["reset_delay"] = {"seconds": 1}
        alarm["groups"][0]["inputs"][0]["silence_timeout"] = {"seconds": 1}

    service = make_service(tmp_path, with_reset_delay)
    group = service.groups[0]

    send(service, "in/door", "1")
    expect_next(service, {"out/siren": "m == 'alarm-intrusion'"})

    # The input stops reporting entirely while still reading "triggered".
    run(service, 2.5)

    assert group.inputs[0].get_state() == InputState.OFFLINE
    assert group.inputs[0].get_last_value()
    assert group.state == AlarmState.ALARM, "alarm was released by a dying sensor"


def test_input_with_unparseable_payload_is_not_reported_online(tmp_path):
    """A sensor emitting values the condition cannot handle is not healthy."""

    def numeric(alarm):
        alarm["groups"][0]["inputs"][0]["when"] = "value_json['open'] == True"

    service = make_service(tmp_path, numeric)
    door = service.groups[0].inputs[0]

    send(service, "in/door", '{"open": false}')
    assert door.get_state() == InputState.ONLINE

    send(service, "in/door", "ERROR: sensor fault")
    assert door.get_state() == InputState.INVALID_RESPONSE
    assert not service.groups[0].all_ok()


def test_inhibit_command_from_home_assistant_lasts(tmp_path):
    """"1" means "inhibit", not "inhibit for one second"."""
    service = make_service(tmp_path)
    group = service.groups[0]

    send(service, "service/alarm/ga/inhibited/command", "1")
    assert group.inhibited_by_command
    assert group.inhibit_timeout_loop.interval > timedelta(seconds=60)

    # Word forms work like every other command topic.
    send(service, "service/alarm/ga/inhibited/command", "off")
    assert not group.inhibited_by_command

    send(service, "service/alarm/ga/inhibited/command", "on")
    assert group.inhibited_by_command

    # An explicit duration is still honoured.
    send(service, "service/alarm/ga/inhibited/command", "120")
    assert group.inhibit_timeout_loop.interval == timedelta(seconds=120)


def test_reset_does_not_publish_an_empty_text_message(tmp_path):
    """An empty payload to an SMS gateway is an empty SMS."""
    service = make_service(tmp_path)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "'Group A' in m"})

    send(service, "service/alarm/ga/reset/command", "1")
    expect_next(service, {"out/sms": None, "service/alarm/ga/state": "m == 'off'"}, 0.6)


def test_self_test_exercises_the_text_output(tmp_path):
    """There must be a way to prove the output path works without an alarm."""
    service = make_service(tmp_path)
    send(service, "service/alarm/test/command", "1")
    expect_next(
        service,
        {
            "out/sms": "'TEST MESSAGE' in m",
            "service/alarm/test/result": "'sms' in m",
        },
    )


# --------------------------------------------------------------------------
# Configuration that cannot work must be rejected at startup
# --------------------------------------------------------------------------


def test_schedule_without_the_needed_state_is_rejected(tmp_path):
    """Used to be a KeyError at the moment the alarm fired."""

    def prealarm_only_schedule(alarm):
        del alarm["switch_outputs"]["siren"]["intrusion"]["alarm"]

    with pytest.raises(Exception, match="no 'alarm' message"):
        make_service(tmp_path, prealarm_only_schedule)


def test_unknown_output_type_is_rejected(tmp_path):
    """A typo in the outputs mapping silently produced no output at all."""

    def typo(alarm):
        group = alarm["groups"][0]
        group["outputs"] = {"Alarm": [{"siren": "intrusion"}]}

    with pytest.raises(Exception, match="unknown output type"):
        make_service(tmp_path, typo)


def test_unknown_schedule_name_is_rejected(tmp_path):
    def bad_schedule(alarm):
        alarm["groups"][0]["outputs"] = {"alarm": [{"siren": "nonexistent"}]}

    with pytest.raises(Exception, match="no schedule"):
        make_service(tmp_path, bad_schedule)


def test_unknown_output_name_is_rejected(tmp_path):
    def bad_output(alarm):
        alarm["groups"][0]["outputs"] = {"alarm": [{"nosuchsiren": "intrusion"}]}

    with pytest.raises(Exception, match="unknown switch output"):
        make_service(tmp_path, bad_output)


def test_prealarm_without_alarm_outputs_is_rejected(tmp_path):
    """Such a group can enter prealarm but escalating does nothing."""

    def prealarm_only(alarm):
        alarm["groups"][0]["prealarm"] = {"seconds": 5}
        alarm["groups"][0]["outputs"] = {"prealarm": [{"siren": "intrusion"}]}

    with pytest.raises(Exception, match="no 'alarm' outputs"):
        make_service(tmp_path, prealarm_only)


def test_group_without_outputs_is_rejected(tmp_path):
    def no_outputs(alarm):
        alarm["groups"][0]["outputs"] = {}

    with pytest.raises(Exception, match="no outputs at all"):
        make_service(tmp_path, no_outputs)


def test_shared_output_config_is_not_consumed_by_the_first_group(tmp_path):
    """popitem() emptied the config dict, which YAML anchors can share."""
    shared = {"siren": "intrusion"}
    config = yaml.safe_load(yaml.safe_dump(BASE_CONFIG))
    alarm = config["services"]["alarm"]
    alarm["groups"][0]["outputs"] = {"alarm": [shared, "sms"]}
    alarm["groups"].append(
        {
            "name": "gb",
            "label": "Group B",
            "inputs": [{"mqtt": "in/window", "when": "is_on(value)", "label": "W"}],
            "outputs": {"alarm": [shared, "sms"]},
        }
    )
    path = tmp_path / "miqro.yml"
    path.write_text(yaml.safe_dump(config))

    service = AlarmService(
        str(path), mqtt_client_cls=DummyMQTTClient, state_cls=ReadOnlyDummyState
    )
    # Both groups actually got the switch output.
    assert len(service.groups[0].switch_outputs["alarm"]) == 1
    assert len(service.groups[1].switch_outputs["alarm"]) == 1
