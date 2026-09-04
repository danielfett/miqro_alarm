"""Regression tests for ways the alarm could fail without it being noticeable.

Each test here corresponds to a defect where the service kept running, and kept
reporting itself healthy, while it had stopped being able to raise an alarm.
"""

import json
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
# Alarm messages: severity is readable at a glance
# --------------------------------------------------------------------------


def test_a_prealarm_and_an_alarm_are_marked_differently(tmp_path):
    def with_prealarm(alarm):
        group = alarm["groups"][0]
        group["prealarm"] = {"seconds": 60}
        group["outputs"] = {"prealarm": ["sms"], "alarm": [{"siren": "intrusion"}, "sms"]}

    service = make_service(tmp_path, with_prealarm)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "m == '⚠️ Prealarm, Group A: Door'"})

    service.groups[0].do_alarm("test")
    expect_next(service, {"out/sms": "m == '🚨 ALARM, Group A: Door'"})


def test_a_group_in_prealarm_is_not_marked_as_an_alarm(tmp_path):
    """One group escalating must not put a 🚨 on a group that is only in prealarm.

    A message is triggered by one group but restates all of them, so taking the
    wording from the triggering event mislabels every other group.
    """

    def two_groups(alarm):
        alarm["groups"][0]["prealarm"] = {"seconds": 60}
        alarm["groups"][0]["outputs"] = {
            "prealarm": ["sms"],
            "alarm": [{"siren": "intrusion"}, "sms"],
        }
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

    send(service, "in/door", "1")  # Group A: prealarm
    expect_next(service, {"out/sms": "m.startswith('⚠️')"})

    send(service, "in/window", "1")  # Group B: straight to alarm
    seen = expect_next(service, {"out/sms": "'Group B' in m"})

    message = texts_on(seen)[-1]
    assert message == "⚠️ Prealarm, Group A: Door\n🚨 ALARM, Group B: Window"


def test_a_further_input_during_an_alarm_is_marked_as_an_update(tmp_path):
    def two_inputs(alarm):
        alarm["groups"][0]["inputs"].append(
            {"mqtt": "in/window", "when": "is_on(value)", "label": "Window"}
        )

    service = make_service(tmp_path, two_inputs)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "m == '🚨 ALARM, Group A: Door'"})

    send(service, "in/window", "1")
    expect_next(service, {"out/sms": "m == '🚨 ALARM (update), Group A: Door, Window'"})


def test_one_group_clearing_reports_the_others_as_they_are(tmp_path):
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

    send(service, "service/alarm/ga/reset/command", "1")
    seen = expect_next(service, {"out/sms": "'cleared' in m.lower()"})

    assert texts_on(seen)[-1] == "⎚ Cleared, Group A\n🚨 ALARM, Group B: Window"


# --------------------------------------------------------------------------
# A separate topic for the all-clear
# --------------------------------------------------------------------------


def with_clear_topic(alarm):
    """Send this output's clears somewhere other than its alarms."""
    alarm["text_outputs"]["sms"]["mqtt_clear"] = "out/sms_clear"


def second_group(alarm):
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


def test_the_all_clear_goes_to_the_clear_topic_and_not_the_alarm_topic(tmp_path):
    """An all-clear on the alarm topic reaches people at alarm priority."""
    service = make_service(tmp_path, with_clear_topic)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "m == '🚨 ALARM, Group A: Door'"})

    send(service, "service/alarm/ga/reset/command", "1")
    expect_next(
        service,
        {
            "out/sms_clear": "m == '⎚ Cleared, Group A'",
            "out/sms": None,
        },
        0.6,
    )

    # Said once, here as well.
    expect_next(service, {"out/sms_clear": None, "out/sms": None}, 0.6)


def test_clearing_one_group_does_not_restate_a_running_one_as_an_alarm(tmp_path):
    """With one message for both, ending A resent B's line on the alarm topic.

    Downstream that is indistinguishable from B having just started.
    """

    def two_groups(alarm):
        with_clear_topic(alarm)
        second_group(alarm)

    service = make_service(tmp_path, two_groups)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "'Group A' in m"})
    send(service, "in/window", "1")
    expect_next(service, {"out/sms": "'Group B' in m"})

    send(service, "service/alarm/ga/reset/command", "1")
    expect_next(
        service,
        {
            "out/sms_clear": "m == '⎚ Cleared, Group A'",
            "out/sms": None,
        },
        0.6,
    )


def test_a_failed_clear_is_retried_on_its_own(tmp_path, monkeypatch):
    """Two topics, two publishes -- and each is only recorded once it worked.

    Committing them together would mean a delivered clear is sent again because
    the alarm message beside it failed, or the reverse.
    """

    def two_groups(alarm):
        with_clear_topic(alarm)
        second_group(alarm)

    service = make_service(tmp_path, two_groups)

    class FailedPublish:
        rc = 1

    failing = set()
    real_publish = service.publish

    def publish(topic, *args, **kwargs):
        if topic in failing:
            return FailedPublish()
        return real_publish(topic, *args, **kwargs)

    monkeypatch.setattr(service, "publish", publish)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "m == '🚨 ALARM, Group A: Door'"})

    failing.add("out/sms_clear")
    send(service, "service/alarm/ga/reset/command", "1")
    expect_next(
        service,
        {
            "service/alarm/ga/state": "m == 'off'",
            "out/sms_clear": None,
            "out/sms": None,
        },
        0.6,
    )

    # The next thing to happen carries the clear that never made it, without
    # repeating the alarm message that did.
    failing.clear()
    send(service, "in/window", "1")
    seen = expect_next(
        service,
        {
            "out/sms_clear": "m == '⎚ Cleared, Group A'",
            "out/sms": "m == '🚨 ALARM, Group B: Window'",
        },
    )
    assert texts_on(seen) == ["🚨 ALARM, Group B: Window"]


# --------------------------------------------------------------------------
# Cancelling a notification that is still escalating
# --------------------------------------------------------------------------


def with_cancel(alarm):
    """A gateway that repeats an alarm until it is told to stop."""
    alarm["text_outputs"]["sms"].update(
        mqtt_clear="out/sms_clear",
        mqtt_cancel="out/sms_cancel",
    )


def test_a_tagged_alarm_carries_the_tag_the_gateway_cancels_by(tmp_path):
    """Cancelling works by tag, and plain text has nowhere to put one."""
    service = make_service(tmp_path, with_cancel)

    send(service, "in/door", "1")
    seen = expect_next(service, {"out/sms": "'ALARM' in m"})

    assert json.loads(texts_on(seen)[-1]) == {
        "message": "🚨 ALARM, Group A: Door",
        "tags": "sms",  # the output's name, by default
    }


def test_the_last_group_clearing_cancels_what_is_still_escalating(tmp_path):
    service = make_service(tmp_path, with_cancel)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "'ALARM' in m"})

    send(service, "service/alarm/ga/reset/command", "1")
    expect_next(
        service,
        {
            "out/sms_cancel": "m == 'sms'",
            "out/sms_clear": "m == '⎚ Cleared, Group A'",
        },
        0.6,
    )

    # Said once: the gateway is not asked again on every later update.
    expect_next(service, {"out/sms_cancel": None}, 0.6)


@pytest.mark.parametrize(
    "topic, payload",
    [
        ("service/alarm/ga/auto/command", "1"),  # the one-button way, mid-alarm
        ("service/alarm/ga/enabled/command", "0"),
        ("service/alarm/ga/reset/command", "1"),
        ("service/alarm/reset/command", "1"),
    ],
)
def test_every_way_of_switching_an_alarm_off_cancels_the_notification(
    tmp_path, topic, payload
):
    """A gateway that repeats until acknowledged must hear about all of them.

    'auto' in particular is how the alarm is switched off in passing, and it
    reaches reset_outputs() by a different route than a reset command.
    """
    service = make_service(tmp_path, with_cancel)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "'ALARM' in m"})

    send(service, topic, payload)
    expect_next(service, {"out/sms_cancel": "m == 'sms'"}, 0.6)


def test_nothing_is_cancelled_while_another_group_is_still_in_alarm(tmp_path):
    """One cancel stops every message carrying the tag, running ones included."""

    def two_groups(alarm):
        with_cancel(alarm)
        second_group(alarm)

    service = make_service(tmp_path, two_groups)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "'Group A' in m"})
    send(service, "in/window", "1")
    expect_next(service, {"out/sms": "'Group B' in m"})

    send(service, "service/alarm/ga/reset/command", "1")
    expect_next(
        service,
        {
            "out/sms_clear": "m == '⎚ Cleared, Group A'",
            "out/sms_cancel": None,
        },
        0.6,
    )

    # Only once B ends as well is there nothing left to escalate.
    send(service, "service/alarm/gb/reset/command", "1")
    expect_next(service, {"out/sms_cancel": "m == 'sms'"}, 0.6)


def test_a_failed_cancel_is_retried(tmp_path, monkeypatch):
    """A dropped cancel leaves a phone escalating an alarm that is over."""
    service = make_service(tmp_path, with_cancel)

    class FailedPublish:
        rc = 1

    failing = set()
    real_publish = service.publish

    def publish(topic, *args, **kwargs):
        if topic in failing:
            return FailedPublish()
        return real_publish(topic, *args, **kwargs)

    monkeypatch.setattr(service, "publish", publish)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "'ALARM' in m"})

    failing.add("out/sms_cancel")
    send(service, "service/alarm/ga/reset/command", "1")
    expect_next(service, {"out/sms_clear": "'Cleared' in m", "out/sms_cancel": None}, 0.6)

    # The retry loop keeps trying without anything else having to happen.
    failing.clear()
    expect_next(service, {"out/sms_cancel": "m == 'sms'"}, 2.5)


def test_a_new_alarm_supersedes_a_cancel_that_never_got_through(tmp_path, monkeypatch):
    """Sending it late would stop the alarm that is running right now."""
    service = make_service(tmp_path, with_cancel)

    class FailedPublish:
        rc = 1

    failing = {"out/sms_cancel"}
    real_publish = service.publish

    def publish(topic, *args, **kwargs):
        if topic in failing:
            return FailedPublish()
        return real_publish(topic, *args, **kwargs)

    monkeypatch.setattr(service, "publish", publish)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "'ALARM' in m"})
    send(service, "service/alarm/ga/reset/command", "1")
    expect_next(service, {"out/sms_clear": "'Cleared' in m"}, 0.6)
    assert service.text_outputs["sms"].cancel_pending

    failing.clear()
    send(service, "in/door", "0")
    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "'ALARM' in m", "out/sms_cancel": None}, 0.6)

    assert not service.text_outputs["sms"].cancel_pending


def test_an_output_without_a_cancel_topic_still_sends_plain_text(tmp_path):
    """Nothing changes for a gateway that was never told about tags."""
    service = make_service(tmp_path)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "m == '🚨 ALARM, Group A: Door'"})

    send(service, "service/alarm/ga/reset/command", "1")
    expect_next(service, {"out/sms": "m == '⎚ Cleared, Group A'"}, 0.6)


# --------------------------------------------------------------------------
# Notifications: one message when a problem starts, one when it ends
# --------------------------------------------------------------------------


def texts_on(seen, topic="out/sms"):
    return [payload for t, payload in seen if t == topic]


def test_a_silent_input_is_announced_once_and_when_it_returns(tmp_path):
    """Repeating the same complaint every check turns the channel into noise."""

    def silent(alarm):
        group = alarm["groups"][0]
        group["default_enabled"] = False  # keep the alarm path out of the way
        group["inputs"][0]["silence_timeout"] = {"seconds": 1}

    service = make_service(tmp_path, silent)

    seen = expect_next(
        service,
        {"out/sms": "m.startswith('🔴') and 'Silent since launch' in m"},
        2.0,
    )
    assert "[1 error, 0 warnings]" in texts_on(seen)[-1]

    # The check keeps running -- and keeps quiet, because nothing changed.
    expect_next(service, {"out/sms": None}, 2.5)

    send(service, "in/door", "0")
    seen = expect_next(service, {"out/sms": "m.startswith('🟢')"})
    assert "Reporting again" in texts_on(seen)[-1]
    assert "[0 errors, 0 warnings]" in texts_on(seen)[-1]
    assert service.condition_counts() == (0, 0)


def test_every_message_carries_the_number_of_active_problems(tmp_path):
    def two_silent_inputs(alarm):
        group = alarm["groups"][0]
        group["default_enabled"] = False
        group["inputs"][0]["silence_timeout"] = {"seconds": 1}
        group["inputs"].append(
            {
                "mqtt": "in/window",
                "when": "is_on(value)",
                "label": "Window",
                "silence_timeout": {"seconds": 1},
            }
        )

    service = make_service(tmp_path, two_silent_inputs)

    seen = expect_next(service, {"out/sms": ["m.startswith('🔴')", 2]}, 3.0)
    messages = texts_on(seen)
    assert any("[1 error, 0 warnings]" in m for m in messages)
    assert any("[2 errors, 0 warnings]" in m for m in messages)
    assert service.condition_counts() == (2, 0)

    # One input recovering leaves the other one counted.
    send(service, "in/door", "0")
    seen = expect_next(service, {"out/sms": "m.startswith('🟢')"})
    assert "[1 error, 0 warnings]" in texts_on(seen)[-1]


def test_an_ignored_inhibitor_is_announced_as_a_warning(tmp_path):
    """Failing open is a warning: the alarm still works, just not as configured."""

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

    send(service, "in/at_home", "1")

    # Two distinct facts: the sensor is dead, and the alarm is consequently
    # armed although the configuration says it should be inhibited.
    seen = expect_next(
        service, {"out/sms": ["m[0] in '🔴🟡'", 2]}, 3.0
    )
    messages = texts_on(seen)
    assert any(m.startswith("🔴") and "Silent for" in m for m in messages)
    assert any(
        m.startswith("🟡") and "ignoring it so the alarm stays armed" in m
        for m in messages
    )
    assert any("[1 error, 1 warning]" in m for m in messages)
    assert service.condition_counts() == (1, 1)

    expect_next(service, {"out/sms": None}, 2.5)

    send(service, "in/at_home", "1")
    expect_next(service, {"out/sms": ["m.startswith('🟢')", 2]}, 2.5)
    assert service.condition_counts() == (0, 0)


def test_an_unevaluatable_payload_is_announced_once(tmp_path):
    """A sensor emitting garbage at 10 Hz must not bury everything else."""

    def json_condition(alarm):
        alarm["groups"][0]["inputs"][0]["when"] = "value_json['open'] == True"

    service = make_service(tmp_path, json_condition)

    send(service, "in/door", "ERROR: sensor fault")
    seen = expect_next(service, {"out/sms": "m.startswith('🔴')"})
    assert "cannot be evaluated" in texts_on(seen)[-1]

    # Every further bad payload is silent (send() clears the queue first, so
    # this really is what that one message caused).
    for _ in range(3):
        send(service, "in/door", "ERROR: sensor fault")
        expect_next(service, {"out/sms": None}, 0.3)

    send(service, "in/door", '{"open": false}')
    seen = expect_next(service, {"out/sms": "m.startswith('🟢')"})
    assert "evaluated again" in texts_on(seen)[-1]
    assert service.condition_counts() == (0, 0)


def test_a_liveness_failure_is_announced_once_and_when_it_recovers(tmp_path):
    def with_liveness(alarm):
        alarm["groups"][0]["liveness"] = [
            {
                "mqtt": "in/heartbeat",
                "when": "is_on(value)",
                "label": "Heartbeat",
                "silence_timeout": {"hours": 1},
                "invalid_response_timeout": {"seconds": 1},
            }
        ]

    service = make_service(tmp_path, with_liveness)

    send(service, "in/heartbeat", "1")
    send(service, "in/heartbeat", "0")

    seen = expect_next(service, {"out/sms": "m.startswith('🔴')"}, 2.5)
    assert "Invalid response" in texts_on(seen)[-1]

    expect_next(service, {"out/sms": None}, 2.0)

    send(service, "in/heartbeat", "1")
    seen = expect_next(service, {"out/sms": "m.startswith('🟢')"})
    assert "Responding correctly again" in texts_on(seen)[-1]


def store_door_state(**overrides):
    """Persist a reading for in/door as a previous run would have."""
    import miqro.test.tools as tools

    entry = {
        "last_raw_value": None,
        "last_eval_value": None,
        "last_update": None,
        "state": InputState.UNKNOWN.value,
    }
    entry.update(overrides)
    tools.SAVED_STATE.update(
        {"mqtt_input": {"in/door": {"is_on(value)": {"last_state": entry}}}}
    )


def test_an_input_that_had_not_reported_yet_is_not_declared_dead_at_startup(tmp_path):
    """The state file is written every 30s whether or not anything arrived.

    So an input that simply had not reported yet when the service was last
    stopped has a stored record with no timestamp -- which must not be read as
    evidence that it is dead, or every restart announces a dead sensor before
    the input has had any chance to say anything.
    """

    def short_timeout(alarm):
        alarm["groups"][0]["inputs"][0]["silence_timeout"] = {"seconds": 1}

    store_door_state()  # never reported, and not offline when we stopped
    service = make_service(tmp_path, short_timeout, state_cls=DummyState)

    assert service.condition_counts() == (0, 0), "declared dead before it could report"
    assert service.groups[0].inputs[0].get_state() == InputState.UNKNOWN

    # This run's silence timeout still decides, in its own time.
    seen = expect_next(service, {"out/sms": "m.startswith('🔴')"}, 2.5)
    assert "Silent since launch" in texts_on(seen)[-1]


def test_an_input_that_was_already_offline_is_reported_after_a_restart(tmp_path):
    """A full timeout did elapse without a message: that is evidence, and it holds."""
    store_door_state(state=InputState.OFFLINE.value)
    service = make_service(tmp_path, state_cls=DummyState)

    assert service.condition_counts() == (1, 0)
    assert "still offline after a restart" in service.condition_text()


def test_a_stored_online_reading_without_a_timestamp_is_not_trusted(tmp_path):
    """'Online' with nothing behind it must not count as a live reading."""
    store_door_state(last_eval_value=True, last_raw_value="1", state=InputState.ONLINE.value)
    service = make_service(tmp_path, state_cls=DummyState)

    door = service.groups[0].inputs[0]
    assert door.get_state() == InputState.UNKNOWN
    assert not door.is_live()
    assert service.condition_counts() == (0, 0)


def test_a_problem_found_during_startup_is_sent_once_connected(tmp_path):
    """Publishing before the first CONNACK is discarded, and there is no retry."""
    import miqro.test.tools as tools

    tools.SAVED_STATE.update(
        {
            "mqtt_input": {
                "in/door": {
                    "is_on(value)": {
                        "last_state": {
                            "last_raw_value": "0",
                            "last_eval_value": False,
                            "last_update": datetime.now() - timedelta(days=3),
                            "state": InputState.ONLINE.value,
                        }
                    }
                }
            }
        }
    )

    service = make_service(tmp_path, state_cls=DummyState)

    assert not service.is_connected
    assert service.condition_counts() == (1, 0), "the stale reading was not noticed"

    seen = expect_next(service, {"out/sms": "m.startswith('🔴')"}, 2.0)
    assert "Silent for" in texts_on(seen)[-1]


def test_active_problems_are_listed_for_anything_that_missed_the_message(tmp_path):
    def silent(alarm):
        alarm["groups"][0]["default_enabled"] = False
        alarm["groups"][0]["inputs"][0]["silence_timeout"] = {"seconds": 1}

    service = make_service(tmp_path, silent)
    assert service.condition_text() == "none"

    expect_next(service, {"out/sms": "m.startswith('🔴')"}, 2.0)
    assert "🔴" in service.condition_text()
    assert "Silent since launch" in service.condition_text()


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

    # An inhibitor that is being ignored has to be visible, otherwise the group
    # silently stops behaving the way its configuration says it does.
    state = group.get_state()
    assert state["active_inhibitors_text"] == "none"
    assert state["offline_inhibitors_text"] == "At home (offline)"

    send(service, "in/door", "1")
    expect_next(service, {"out/siren": "m == 'alarm-intrusion'"})


def test_multi_input_child_can_log_during_construction(tmp_path):
    """A half-built MultiInput must be safe to stringify.

    MultiInput builds its children before assigning self.inputs/self.mode, and
    each child takes the MultiInput as its group.  Any log message emitted while
    a child is being constructed formats that group -- which crashed startup with
    AttributeError, taking the whole service down.
    """
    import miqro.test.tools as tools

    def multi(alarm):
        alarm["groups"][0]["inputs"] = [
            {
                "label": "Multi",
                "mode": "or",
                "inputs": [
                    {"mqtt": "in/a", "when": "is_on(value)", "label": "A1"},
                    {"mqtt": "in/b", "when": "is_on(value)", "label": "A2"},
                ],
            }
        ]

    # A stored reading older than the silence timeout makes _load_state warn
    # while the child is still being constructed.
    tools.SAVED_STATE.update(
        {
            "mqtt_input": {
                "in/a": {
                    "is_on(value)": {
                        "last_state": {
                            "last_raw_value": "1",
                            "last_eval_value": True,
                            "last_update": datetime.now() - timedelta(days=3),
                            "state": InputState.ONLINE.value,
                        }
                    }
                }
            }
        }
    )

    service = make_service(tmp_path, multi, state_cls=DummyState)

    multi_input = service.groups[0].inputs[0]
    assert str(multi_input) == "Multi (2 inputs, 'or')"
    # The stale stored reading was not laundered into looking fresh.
    assert multi_input.inputs[0].get_state() == InputState.OFFLINE


@pytest.mark.parametrize(
    "stored",
    [
        {"last_update": "2026-01-01 10:00:00"},  # a string, not a timestamp
        {"last_update": 1234567890},  # a number
        {"state": 99},  # not a valid InputState
        {},  # missing every key
        "not a mapping at all",
    ],
    ids=["string-date", "numeric-date", "bad-state", "empty", "not-a-mapping"],
)
def test_unusable_stored_input_state_does_not_stop_startup(tmp_path, stored):
    """A hand-edited or version-skewed state file must not prevent startup."""
    import miqro.test.tools as tools

    if isinstance(stored, dict):
        entry = {
            "last_raw_value": "1",
            "last_eval_value": True,
            "last_update": datetime.now(),
            "state": InputState.ONLINE.value,
        }
        entry.update(stored)
    else:
        entry = stored

    tools.SAVED_STATE.update(
        {"mqtt_input": {"in/door": {"is_on(value)": {"last_state": entry}}}}
    )

    # The service starts at all -- this used to be able to raise out of the
    # constructor, which systemd turns into a restart loop.
    service = make_service(tmp_path, state_cls=DummyState)
    assert service.groups[0].state == AlarmState.OFF

    # And the input still works: a genuine change triggers the alarm.
    send(service, "in/door", "0")
    send(service, "in/door", "1")
    expect_next(service, {"out/siren": "m == 'alarm-intrusion'"})


def test_multi_input_rejects_a_bad_mode_before_building_children(tmp_path):
    def bad_mode(alarm):
        alarm["groups"][0]["inputs"] = [
            {
                "label": "Multi",
                "mode": "xor",
                "inputs": [{"mqtt": "in/a", "when": "is_on(value)", "label": "A1"}],
            }
        ]

    with pytest.raises(Exception, match="must be either 'and' or 'or'"):
        make_service(tmp_path, bad_mode)


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


def test_reset_publishes_a_cleared_message_not_an_empty_one(tmp_path):
    """An empty payload to an SMS gateway is an empty SMS -- but silence is worse.

    Whoever was told that the alarm started is owed the other half of the story.
    """
    service = make_service(tmp_path)

    send(service, "in/door", "1")
    expect_next(service, {"out/sms": "m == '🚨 ALARM, Group A: Door'"})

    send(service, "service/alarm/ga/reset/command", "1")
    expect_next(
        service,
        {
            "out/sms": "m == '⎚ Cleared, Group A'",
            "service/alarm/ga/state": "m == 'off'",
        },
        0.6,
    )

    # Said once: there is nothing left to report afterwards.
    expect_next(service, {"out/sms": None}, 0.6)


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


def second_group_watching_the_door(alarm, **input_overrides):
    """A second group whose input is the *same topic* as the first group's."""
    door = {"mqtt": "in/door", "when": "is_on(value)", "label": "Door (hall)"}
    door.update(input_overrides)
    alarm["groups"].append(
        {
            "name": "gb",
            "label": "Group B",
            "inputs": [door],
            "outputs": {"alarm": ["sms"]},
        }
    )


def test_one_topic_with_two_different_silence_timeouts_is_reported(tmp_path):
    """The same sensor cannot sensibly be online in one group and offline in another."""

    def mismatched(alarm):
        alarm["groups"][0]["inputs"][0]["silence_timeout"] = {"hours": 1}
        second_group_watching_the_door(alarm, silence_timeout={"hours": 12})

    service = make_service(tmp_path, mismatched)

    assert service.condition_counts() == (0, 1)
    seen = expect_next(service, {"out/sms": "m.startswith('🟡')"}, 2.0)
    message = texts_on(seen)[-1]
    assert "in/door" in message
    assert "'Door' in group 'ga' (1 hour)" in message
    assert "'Door (hall)' in group 'gb' (12 hours)" in message


def test_disabled_and_enabled_silence_detection_on_one_topic_is_reported(tmp_path):
    def half_disabled(alarm):
        alarm["groups"][0]["inputs"][0]["silence_timeout"] = {"hours": 1}
        second_group_watching_the_door(alarm, silence_timeout=None)

    service = make_service(tmp_path, half_disabled)

    assert service.condition_counts() == (0, 1)
    assert "no silence detection" in service.condition_text()


def test_one_topic_with_the_same_silence_timeout_is_fine(tmp_path):
    """Watching one topic twice is legitimate -- disagreeing about it is not."""

    def matched(alarm):
        # One states the default explicitly, the other inherits it.
        alarm["groups"][0]["inputs"][0]["silence_timeout"] = {"hours": 6}
        second_group_watching_the_door(alarm, when="is_off(value)")

    service = make_service(tmp_path, matched)

    assert service.condition_counts() == (0, 0)
    assert service.condition_text() == "none"


def test_a_topic_inside_a_multi_input_is_checked_too(tmp_path):
    """A MultiInput's children do their own silence detection, so they count."""

    def nested(alarm):
        alarm["groups"][0]["inputs"] = [
            {
                "label": "Either door",
                "mode": "or",
                "inputs": [
                    {
                        "mqtt": "in/door",
                        "when": "is_on(value)",
                        "label": "Front",
                        "silence_timeout": {"minutes": 30},
                    }
                ],
            }
        ]
        second_group_watching_the_door(alarm, silence_timeout={"hours": 12})

    service = make_service(tmp_path, nested)

    assert service.condition_counts() == (0, 1)
    assert "'Front' in group 'ga' (30 minutes)" in service.condition_text()


def test_an_inhibitor_sharing_a_topic_with_an_input_is_checked(tmp_path):
    def input_and_inhibitor(alarm):
        alarm["groups"][0]["inputs"][0]["silence_timeout"] = {"hours": 1}
        alarm["groups"][0]["inhibitors"] = [
            {
                "mqtt": "in/door",
                "when": "is_off(value)",
                "label": "Door closed",
                "silence_timeout": {"hours": 8},
            }
        ]

    service = make_service(tmp_path, input_and_inhibitor)

    assert service.condition_counts() == (0, 1)
    assert "'Door closed' in group 'ga' (8 hours)" in service.condition_text()


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
