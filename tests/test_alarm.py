import pytest
from miqro.test.tools import *
from miqro_alarm.alarm import AlarmService
from logging import getLogger

log = getLogger("test_alarm")


@pytest.fixture(scope="function")
def service():
    svc = AlarmService(
        "tests/miqro.yml", mqtt_client_cls=DummyMQTTClient, state_cls=ReadOnlyDummyState
    )
    return svc


@pytest.fixture(scope="function")
def service_no_info_interval():
    svc = AlarmService(
        "tests/miqro.yml", mqtt_client_cls=DummyMQTTClient, state_cls=ReadOnlyDummyState
    )
    svc.debug_suppress_info_publish = True
    return svc


def test_two_services():
    a = AlarmService(
        "tests/miqro.yml", mqtt_client_cls=DummyMQTTClient, state_cls=ReadOnlyDummyState
    )
    b = AlarmService(
        "tests/miqro.yml", mqtt_client_cls=DummyMQTTClient, state_cls=ReadOnlyDummyState
    )
    assert a.mqtt_client is not b.mqtt_client
    assert a.mqtt_client.message_queue == b.mqtt_client.message_queue
    assert a.mqtt_client.subscribed == b.mqtt_client.subscribed
    assert len(a.LOOPS) == len(b.LOOPS)
    assert a.LOOPS != b.LOOPS
    expect_next(a, {"service/alarm/online": "m"})
    expect_next(b, {"service/alarm/online": "m"})


def test_service_comes_online(service):
    expect_next(service, {"service/alarm/online": "m"})


def test_alarm_information_published(service):
    expect_next(
        service,
        {
            "service/alarm/g1/state": 'm == "off"',
            "service/alarm/g1/enabled/state": 'm == "0"',
            "service/alarm/g1/inhibited/state": 'm == "0"',
            "service/alarm/g1/all_inputs_online": 'm == "0"',
            "service/alarm/g1/input/Input 1/state": 'm == "unknown"',
            "service/alarm/g1/input/Input 2/state": 'm == "unknown"',
        },
    )
    send(service, "service/alarm/g1/enabled/command", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/enabled/state": 'm == "1"',
        },
    )


def test_alarm_default_on(service):
    expect_next(service, {"service/alarm/g2/enabled/state": 'm == "1"'})


def test_alarm_saves_state(service):
    send(service, "group1/input2", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/enabled/state": 'm == "0"',
            "service/alarm/g2/enabled/state": 'm == "1"',
            "service/alarm/g1/input/Input 2/state": 'm == "online"',
            "service/alarm/g1/input/Input 2/value": 'm == "1"',
        },
    )

    send(service, "service/alarm/g1/enabled/command", "1")
    send(service, "service/alarm/g2/enabled/command", "0")

    run(service, 0.5)

    new_service = AlarmService(
        "tests/miqro.yml", mqtt_client_cls=DummyMQTTClient, state_cls=DummyState
    )

    expect_next(
        new_service,
        {
            "service/alarm/g1/input/Input 2/state": 'm == "online"',
            "service/alarm/g1/input/Input 2/value": 'm == "1"',
            "service/alarm/g1/enabled/state": 'm == "1"',
            "service/alarm/g2/enabled/state": 'm == "0"',
        },
    )


def test_input_goes_on_and_times_out(service):
    expect_next(
        service,
        {
            "service/alarm/g1/input/Input 2/state": 'm == "unknown"',
        },
    )

    send(service, "group1/input2", "1")
    service.publish_info()
    expect_next(
        service,
        {
            "service/alarm/g1/input/Input 2/state": 'm == "online"',
        },
    )

    run(service, 2)
    service.publish_info()
    expect_next(
        service,
        {
            "service/alarm/g1/input/Input 2/state": 'm == "offline"',
        },
    )

    send(service, "group1/input2", "1")
    service.publish_info()
    expect_next(
        service,
        {
            "service/alarm/g1/input/Input 2/state": 'm == "online"',
        },
    )


def test_liveness_stays_silent(service):
    # check state output
    expect_next(
        service,
        {
            "service/alarm/g2/live": 'm == "0"',
        },
    )
    send(service, "group2/liveness1", "1")
    send(service, "group2/liveness2", "1")
    service.publish_info()
    expect_next(
        service,
        {
            "service/alarm/g2/live": 'm == "1"',
        },
    )
    # wait a second and no info message should be sent
    send(service, "group1/input2", "0")  # silence input 2
    expect_next(
        service,
        {
            "text/to1": None,
        },
        0.7,
    )
    send(service, "group1/input2", "0")  # silence input 2
    expect_next(
        service,
        {
            "text/to1": None,
        },
        0.7,
    )


def test_liveness_invalid_response(service):
    # send a failed message and wait for timeout
    send(service, "group2/liveness1", "0")
    send(service, "group2/liveness2", "1")
    send(service, "group1/input2", "0")  # silence input 2
    expect_next(
        service,
        {
            "text/to1": None,
        },
        0.8,
    )
    send(service, "group1/input2", "0")  # silence input 2
    expect_next(
        service,
        {
            "text/to1": "'Invalid response' in m",
        },
        0.5,
    )

    service.publish_info()
    expect_next(
        service,
        {
            "service/alarm/g2/live": None,  # no change
        },
    )


def test_liveness_timeout(service):
    # test silence
    send(service, "group2/liveness1", "1")
    send(service, "group2/liveness2", "1")
    send(service, "group1/input2", "0")  # silence input 2
    expect_next(
        service,
        {
            "text/to1": None,
        },
        0.9,
    )
    send(service, "group1/input2", "0")  # silence input 2
    expect_next(
        service,
        {
            "text/to1": None,
        },
        0.9,
    )
    send(service, "group1/input2", "0")  # silence input 2
    expect_next(
        service,
        {
            "text/to1": "'Liveness 1: Silent for 2' in m",
        },
        0.5,
    )


def test_alarm_transition(service):
    send(service, "service/alarm/g1/enabled/command", "1")
    send(service, "group1/input2", "0")  # silence input 2
    send(service, "group1/input1", "1")
    # expect sw1 three times in a row
    expect_next(
        service,
        {
            "service/alarm/g1/state": 'm == "prealarm"',
            "switch/sw1": ["m == 'schedule1-prealarm'", 3],
            "switch/sw2": None,
        },
        1.9,
    )
    send(service, "group2/liveness1", "0")  # silence input 2
    send(service, "group2/liveness2", "0")  # silence input 2
    send(service, "group1/input2", "0")  # silence input 2
    run(service, 0.5)
    expect_next(
        service,
        {
            "service/alarm/g1/state": 'm == "alarm"',
            "switch/sw1": "m in ['schedule1-reset', 'schedule2-alarm']",
            "text/to1": "'ALARM' in m",
        },
    )

    send(service, "service/alarm/g1/reset/command", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/state": 'm == "off"',
            "switch/sw1": "m == 'schedule2-reset'",
        },
    )


def test_alarm_direct_to_main(service):
    send(service, "service/alarm/g3/enabled/command", "1")
    send(service, "group3/input1", "1")
    expect_next(
        service,
        {
            "service/alarm/g3/state": 'm == "alarm"',
            "text/to1": "'ALARM' in m",
        },
    )
    send(service, "service/alarm/g3/reset/command", "1")
    expect_next(
        service,
        {
            "service/alarm/g3/state": 'm == "off"',
        },
    )


def test_reset_delay_works(service_no_info_interval):
    service = service_no_info_interval
    send(service, "service/alarm/g2/enabled/command", "1")
    send(service, "group2/input1", "1")
    # expect sw2 turning on
    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "prealarm"',
            "switch/sw2": "m == 'schedule3-prealarm'",
        },
    )
    send(service, "group2/input1", "0")
    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "off"',
            "switch/sw2": "m == 'schedule3-reset'",
        },
    )
    # same for alarm
    send(service, "group2/input1", "1")

    # expect sw2 three times in a row
    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "prealarm"',
            "switch/sw2": "m == 'schedule3-prealarm'",
        },
    )
    # wait until alarm is triggered
    expect_next(
        service,
        {
            "switch/sw2": "m == 'schedule3-reset'",
        },
        6,
    )
    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "alarm"',
            "switch/sw2": "m == 'schedule3-alarm'",
        },
    )
    send(service, "group2/input1", "0")
    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "off"',
            "switch/sw2": "m == 'schedule3-reset'",
        },
        6,
    )


def test_no_reset_for_default_alarms(service):
    # inverse to above
    send(service, "service/alarm/g3/enabled/command", "1")
    send(service, "group3/input1", "1")
    run(service, 5)
    expect_next(
        service,
        {
            "switch/sw1": ["m == 'schedule2-alarm'", 2],
        },
    )


def test_display_state(service):
    send(service, "service/alarm/g1/enabled/command", "0")
    expect_next(
        service,
        {
            "service/alarm/g1/display_state": "m == 'disabled'",
        },
    )

    send(service, "service/alarm/g1/enabled/command", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/display_state": "m == 'enabled'",
        },
    )

    send(service, "service/alarm/g1/inhibited/command", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/display_state": "m == 'inhibited'",
        },
    )

    send(service, "service/alarm/g1/inhibited/command", "0")
    expect_next(
        service,
        {
            "service/alarm/g1/display_state": "m == 'enabled'",
        },
    )

    send(service, "group1/input1", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/display_state": "m == 'prealarm'",
        },
    )

    run(service, 1)
    expect_next(
        service,
        {
            "service/alarm/g1/display_state": "m == 'alarm'",
        },
    )

    send(service, "service/alarm/g1/inhibited/command", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/display_state": None,  # remains unchanged
        },
    )


def test_alarm_not_triggered_when_disabled(service_no_info_interval):
    service = service_no_info_interval
    send(service, "service/alarm/g1/enabled/command", "0")
    send(service, "group1/input1", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/state": "m == 'off'",
            "switch/sw1": None,
            "switch/sw2": None,
        },
        2,
    )


def test_alarm_not_triggered_by_inhibitor(service_no_info_interval):
    service = service_no_info_interval
    send(service, "service/alarm/g1/enabled/command", "1")
    send(service, "group1/inhibitor1", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/state": "m == 'off'",
            "switch/sw1": None,
        },
        1,
    )


def test_alarm_not_triggered_when_inhibited_by_timer(service_no_info_interval):
    service = service_no_info_interval
    send(service, "service/alarm/g1/enabled/command", "1")
    send(service, "service/alarm/g1/inhibited/command", "1")
    send(service, "group1/input1", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/state": "m == 'off'",
            "switch/sw1": None,
            "switch/sw2": None,
        },
        2,
    )


def test_alarm_not_triggered_when_inhibited_by_inhibitor(service_no_info_interval):
    service = service_no_info_interval
    send(service, "service/alarm/g1/enabled/command", "1")

    # inhibitor before alarm: alarm should not be triggered
    send(service, "group1/inhibitor1", "1")
    send(service, "group1/input1", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/state": "m == 'off'",
            "switch/sw1": None,
            "switch/sw2": None,
        },
        1,
    )
    send(service, "group1/input1", "0")

    # inhibitor after alarm: prealarm should be triggered
    # afterwards, inhibitor comes online and stops prealarm
    send(service, "group1/inhibitor1", "0")
    send(service, "group1/input1", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/state": "m == 'prealarm'",
        },
    )
    send(service, "group1/inhibitor1", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/state": "m == 'off'",
        },
    )


def test_alarm_updates_received(service):
    # trigger alarm using one input
    # check message received
    # trigger alarm using other input
    # check updated message received
    # do it again
    # check that number of triggers is correct

    send(service, "group1/input2", "0")
    send(service, "group2/input2", "0")
    send(service, "group2/input1", "1")
    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "prealarm"',
            "text/to1": "'Input 1' in m and not 'Input 2' in m",
        },
    )
    # assert service.mqtt_client.message_queue == []
    send(service, "group1/input2", "0")
    send(service, "group2/input2", "1")
    expect_next(
        service,
        {
            "text/to1": "'Input 2' in m and 'Input 1' in m",
        },
    )
    # assert service.mqtt_client.message_queue == []
    send(service, "group1/input2", "0")
    send(service, "group2/input1", "0")
    expect_next(
        service,
        {
            "text/to1": "'Input 2' in m and not 'Input 1' in m",
        },
    )
    # assert service.mqtt_client.message_queue == []


def test_conflicting_alarms(service_no_info_interval):
    service = service_no_info_interval
    # two alarms on the same output
    # trigger both alarms
    # check that the correct schedule is ran
    # stop lower-prio alarm, other alarm schedule should still run
    # start lower-prio alarm again
    # check that the correct schedule is ran
    # stop higher-prio alarm, other alarm schedule should still run
    # check that correct schedule is ran
    # stop alarms
    # check that outputs are stopped

    send(service, "service/alarm/g1/enabled/command", "1")
    send(service, "group1/input1", "1")
    # expect sw1 three times in a row
    expect_next(
        service,
        {
            "service/alarm/g1/state": 'm == "prealarm"',
            "switch/sw1": ["m == 'schedule1-prealarm'", 1],
        },
    )

    send(service, "group2/input1", "1")

    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "prealarm"',
            "switch/sw1": "m == 'schedule1-reset'",
        },
    )
    expect_next(
        service,
        {
            "switch/sw1": "m == 'schedule2-prealarm'",
        },
    )
    send(service, "service/alarm/g2/reset/command", "1")
    expect_next(
        service,
        {
            "switch/sw1": "m == 'schedule2-reset'",
        },
    )

    expect_next(
        service,
        {
            "switch/sw1": ["m == 'schedule1-prealarm'", 3],
        },
    )

    expect_next(
        service,
        {
            "switch/sw1": "m == 'schedule1-reset'",
        },
    )

    expect_next(
        service,
        {
            "switch/sw1": "m == 'schedule2-alarm'",
        },
    )

    send(service, "group2/input1", "0")
    send(service, "group2/input1", "1")
    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "prealarm"',
            "switch/sw1": "m == 'schedule2-reset'",
        },
    )
    expect_next(
        service,
        {
            "switch/sw1": "m == 'schedule2-prealarm'",
        },
    )
    send(service, "service/alarm/g2/reset/command", "1")

    expect_next(
        service,
        {
            "switch/sw1": "m == 'schedule2-reset'",
        },
    )

    expect_next(
        service,
        {
            "switch/sw1": "m == 'schedule2-alarm'",
        },
    )


def test_parallel_mqtt_subscriptions(service_no_info_interval):
    service = service_no_info_interval
    send(service, "service/alarm/g1/enabled/command", "1")
    send(service, "service/alarm/g2/enabled/command", "1")

    send(service, "shared/input0", "1")
    expect_next(
        service,
        {
            "service/alarm/g1/state": 'm == "prealarm"',
            "service/alarm/g2/state": 'm == "prealarm"',
        },
    )


def test_alarm_input_or(service):
    send(service, "group2/multi2/input2", "1")
    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "prealarm"',
        },
        2,
    )

    send(service, "group2/multi2/input2", "0")
    send(service, "service/alarm/g2/reset/command", "1")
    run(service, 0.5)
    send(service, "group2/multi2/input2", "0")
    send(service, "group2/multi2/input1", "1")

    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "prealarm"',
        },
        2,
    )

    send(service, "group2/multi2/input1", "0")
    send(service, "service/alarm/g2/reset/command", "1")
    run(service, 0.5)

    send(service, "group2/multi2/input2", "1")
    send(service, "group2/multi2/input1", "1")

    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "prealarm"',
        },
        2,
    )


def test_alarm_input_and(service):
    send(service, "group2/multi1/input2", "1")
    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "off"',
        },
        2,
    )

    send(service, "group2/multi1/input1", "1")
    expect_next(
        service,
        {
            "service/alarm/g2/state": 'm == "prealarm"',
        },
        2,
    )


def test_debounce(service):
    send(service, "service/alarm/g4/enabled/command", "1")
    send(service, "group4/input1", "1")
    # do not expect any message in the next 0.8 seconds
    expect_next(
        service,
        {
            "service/alarm/g4/state": 'm == "off"',
            "switch/sw1": None,
        },
        0.8,
    )
    # reset input to previous value - nothing should happen
    send(service, "group4/input1", "0")
    # do not expect any message in the next 2 seconds
    expect_next(
        service,
        {
            "service/alarm/g4/state": None,
            "switch/sw1": None,
        },
        2,
    )
    # trigger the alarm again, but now wait a second afterwards
    send(service, "group4/input1", "1")
    # do not expect any message in the next 0.8 seconds
    run(service, 0.9)

    # expect sw1 three times in a row
    expect_next(
        service,
        {
            "service/alarm/g4/state": 'm == "alarm"',
        },
        1.9,
    )

