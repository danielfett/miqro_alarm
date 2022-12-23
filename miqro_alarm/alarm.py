import miqro
import requests
from typing import Optional, Dict, List, Tuple, Union
from datetime import timedelta, datetime
from enum import Enum
from dataclasses import dataclass, field
from heapq import heappush
from humanfriendly import format_timespan
from json import loads


class SwitchOutput:
    service: "AlarmService"
    loop: Optional[miqro.Loop] = None
    message: Optional[str]
    mqtt: Optional[str]
    http_post: Optional[str]

    def __init__(self, service, mqtt=None, message=None, http_post=None, repeat=None):
        if mqtt and not message:
            raise Exception("mqtt is set but message is not")

        self.service = service
        self.mqtt = mqtt
        self.message = message
        self.http_post = http_post
        self.repeat = repeat

        if self.repeat:
            self.loop = miqro.Loop(self._send, timedelta(**self.repeat), False)
            self.service.add_loop(self.loop)

    def _send(self, _=None):
        if self.mqtt and self.message:
            self.service.publish(self.mqtt, self.message, global_=True)
        if self.http_post:
            try:
                requests.post(self.http_post, timeout=10)
            except Exception as e:
                self.service.log.error(f"Error posting to {self.http_post}: {e}")

    def on(self):
        if self.repeat:
            assert self.loop
            self.loop.start()
        else:
            self._send()

    def off(self):
        if self.repeat:
            assert self.loop
            self.loop.stop()


class AlarmState(Enum):
    OFF = 0
    PREALARM = 1
    ALARM = 2


# Sortable alarm requests
@dataclass(order=True)
class AlarmRequest:
    group: "AlarmGroup"
    state: AlarmState = field(compare=False)
    schedule: Optional[str] = field(compare=False)


class SwitchOutputGroup:
    service: "AlarmService"

    schedules: Dict[str, Dict[AlarmState, SwitchOutput]]
    resets: Dict[str, SwitchOutput]

    requests: List[AlarmRequest]
    state: AlarmState = AlarmState.OFF
    current_schedule: Optional[str] = None

    def __init__(self, service, **schedules: Dict[str, Dict]):
        self.service = service
        self.requests = []
        self.schedules = {}
        self.resets = {}

        for name, s in schedules.items():
            schedule = {}
            if "prealarm" in s:
                schedule[AlarmState.PREALARM] = SwitchOutput(service, **s["prealarm"])
            if "alarm" in s:
                schedule[AlarmState.ALARM] = SwitchOutput(service, **s["alarm"])
            if "reset" in s:
                self.resets[name] = SwitchOutput(service, **s["reset"])
            self.schedules[name] = schedule

    def _switch_off(self):
        if self.state == AlarmState.OFF:
            return
        assert self.current_schedule
        self.schedules[self.current_schedule][self.state].off()
        if self.current_schedule in self.resets:
            self.resets[self.current_schedule].on()
        self.state = AlarmState.OFF

    def _switch_on(self, target: AlarmRequest):
        assert target.schedule
        if self.current_schedule and self.current_schedule in self.resets:
            self.resets[self.current_schedule].off()
        self.schedules[target.schedule][target.state].on()
        self.current_schedule = target.schedule
        self.state = target.state

    def request(self, group, state: AlarmState, schedule: Optional[str]):
        self.service.log.info(
            f"Output {self} | Request {state.name} for group: {group}"
        )
        for request in self.requests:
            if request.group == group:
                del self.requests[self.requests.index(request)]

        if state != AlarmState.OFF:
            heappush(self.requests, AlarmRequest(group, state, schedule))

        if len(self.requests) == 0:
            self.service.log.info(f"Output {self} | No requests, setting state to OFF")
            self._switch_off()
            return

        target = self.requests[0]

        if self.current_schedule == target.schedule and self.state == target.state:
            self.service.log.info(
                f"Output {self} | Request {state.name} for group: {group} ignored, already in state: {self.state}"
            )
            return

        # if we're here, there is either a different schedule or a different state. We need to switch off the current
        # schedule and switch on the new one.
        self._switch_off()
        self._switch_on(target)


class TextOutput:
    service: "AlarmService"
    groups: List["AlarmGroup"]
    mqtt: Optional[str]
    info: bool

    published_alarm_information: Optional[Dict] = None

    def __init__(self, service, mqtt, info=False):
        self.service = service
        self.mqtt = mqtt
        self.info = info
        self.groups = []

    def add_group(self, group: "AlarmGroup"):
        if not group in self.groups:
            heappush(self.groups, group)

    def update(self):
        alarm_information = {
            group.label: self._get_group_information(group)
            for group in self.groups
            if group.state in [AlarmState.ALARM, AlarmState.PREALARM]
        }

        self.service.log.info(
            f"TextOutput {self} | Alarm information: {alarm_information}"
        )

        if alarm_information != self.published_alarm_information:
            self.published_alarm_information = alarm_information
            self.service.publish(
                self.mqtt, self._format_msg(alarm_information), global_=True
            )

    def send_info(self, message):
        self.service.publish(self.mqtt, message, global_=True)

    def _get_group_information(self, group: "AlarmGroup"):
        return {
            "state": group.state.name,
            "inputs": [
                {
                    "name": str(input),
                }
                for input in group.inputs
                if input.get_last_value()
            ],
        }

    def _format_msg(self, alarm_information):
        output_strings = []
        for label, g in alarm_information.items():
            group_info = f"{g['state']} {label}: "
            group_info += ", ".join(input["name"] for input in g["inputs"])
            output_strings.append(group_info)
        return "\n".join(output_strings)


def is_on(value):
    return value.lower() in [
        "1",
        "yes",
        "on",
        "true",
    ]


def is_off(value):
    return not is_on(value)


class InputState(Enum):
    INVALID_RESPONSE = -2
    UNKNOWN = -1
    OFFLINE = 0
    ONLINE = 1


class Input:
    service: "AlarmService"
    group: "AlarmGroup"
    label: str

    last_eval_value: Optional[bool] = None
    last_update: Optional[datetime] = None
    state: InputState = InputState.UNKNOWN

    debounce_timeout_check_loop: Optional[miqro.Loop] = None
    debounce_observed_value = None

    def __init__(self, service, group, label, debounce=None):
        self.service = service
        self.group = group
        self.label = label

        if debounce:
            # create debounce loop
            self.debounce_timeout_check_loop = miqro.Loop(
                self._debounce_timeout_check, timedelta(**debounce), start=False
            )
            self.service.add_loop(self.debounce_timeout_check_loop)

    def get_last_value(self):
        return self.last_eval_value

    def get_state(self):
        return self.state

    def __str__(self):
        return self.label

    @staticmethod
    def create(service, group, **kwargs):
        if "mqtt" in kwargs:
            return MQTTInput(service, group, **kwargs)
        else:
            return MultiInput(service, group, **kwargs)

    @staticmethod
    def create_from_input_list(
        service, group, list
    ) -> List[Union["MQTTInput", "MultiInput"]]:
        return [Input.create(service, group, **l) for l in list]

    @staticmethod
    def create_from_liveness_input_list(service, group, list) -> List["LivenessInput"]:
        return [LivenessInput(service, group, **l) for l in list]

    def _handle_change(self, new_eval_value):
        if not self.debounce_timeout_check_loop:
            if new_eval_value == self.last_eval_value:
                return False
            self.service.log.debug(
                f"Group {self.group}, input {self} | No debounce, committing directly"
                    
            )
            self._commit(new_eval_value)
        else:
            if self.debounce_observed_value is None:
                # no state change observed yet, see if the new value changes state
                if new_eval_value is not self.last_eval_value:
                    # if yes, start the observation
                    self.debounce_observed_value = new_eval_value
                    self.debounce_timeout_check_loop.start(delayed=True)
                    self.service.log.debug(
                        f"Group {self.group}, input {self} | Value changed to {new_eval_value}, but waiting for debounce timeout"
                    )
                else:
                    # if not, ignore, as there was no value change
                    self.service.log.debug(
                        f"Group {self.group}, input {self} | Value changed to {new_eval_value}, but same as before - ignoring"
                    )
                    pass
            else:
                # An observation is running already. There are two cases:

                # 1. The new value is the same as the pre-observation value. We reset the observation.
                if new_eval_value is not self.debounce_observed_value:
                    self.debounce_observed_value = None
                    self.debounce_timeout_check_loop.stop()
                    self.service.log.debug(
                        f"Group {self.group}, input {self} | Value changed back to {new_eval_value}, stopped debounce observation"
                    )

                # 2. Otherwise, continue observation.
                else:
                    self.service.log.debug(
                        f"Group {self.group}, input {self} | Value changed to {new_eval_value}, same as running observation"
                    )
                    pass
        return True

    def _commit(self, new_eval_value):
        self.service.log.info(
            f"Group {self.group}, input {self} | Evaluated value changed to {new_eval_value}"
        )
        self.last_eval_value = new_eval_value
        if new_eval_value:
            self.group.on(self)
        else:
            self.group.off(self)

    def _debounce_timeout_check(self, _):
        self.service.log.info(
            f"Group {self.group}, input {self} | Observation timed out, comitting {self.debounce_observed_value}"
        )
        self._commit(self.debounce_observed_value)
        self.debounce_observed_value = None
        return False  # stop loop


class MultiInput(Input):
    def __init__(self, service, group, label, inputs, mode):
        super().__init__(service, group, label)
        self.inputs = Input.create_from_input_list(service, self, inputs)
        if not mode in ["and", "or"]:
            raise Exception(
                f"For multi input, mode must be either 'and' or 'or', but not '{mode}'"
            )

        self.mode = mode

    def get_last_value(self):
        if self.mode == "and":
            return all(i.get_last_value() for i in self.inputs)
        else:
            return any(i.get_last_value() for i in self.inputs)

    def get_state(self):
        collected_states = list(map(lambda i: i.get_state(), self.inputs))
        if InputState.INVALID_RESPONSE in collected_states:
            return InputState.INVALID_RESPONSE
        if InputState.OFFLINE in collected_states:
            return InputState.OFFLINE
        if InputState.ONLINE in collected_states:
            return InputState.ONLINE
        return InputState.UNKNOWN

    def on(self, input):
        new_eval_value = self.get_last_value()
        self._handle_change(new_eval_value)

    def off(self, input):
        self.on(input)

    def __str__(self):
        return f"{self.label} ({len(self.inputs)} inputs, '{self.mode}')"


class MQTTInput(Input):
    mqtt: str
    condition: str
    format: Optional[str]

    silence_timeout_check_loop: Optional[miqro.Loop] = None

    last_raw_value: Optional[str] = None

    def __init__(
        self,
        service,
        group,
        mqtt,
        *,
        when,
        label,
        debounce=None,
        format=None,
        silence_timeout: Optional[Dict] = {"days": 7},
    ):
        super().__init__(service, group, label, debounce)
        self.mqtt = mqtt
        self.condition = when
        self.format = format

        self.service.add_global_handler(self.mqtt, self.handle)

        if silence_timeout is not None:
            self.silence_timeout_check_loop = miqro.Loop(
                self._check_silence_timeout, timedelta(**silence_timeout), False
            )
            self.service.add_loop(self.silence_timeout_check_loop)
            self.silence_timeout_check_loop.start(delayed=True)

        self._load_state()

        self.store_state_loop = miqro.Loop(
            self._store_state, timedelta(seconds=30), False
        )
        self.service.add_loop(self.store_state_loop)
        self.store_state_loop.start(delayed=True)

    def handle(self, _, raw_value):
        self.last_update = datetime.now()
        self.last_raw_value = raw_value
        if self.silence_timeout_check_loop:
            self.silence_timeout_check_loop.restart(delayed=True)
        self.state = InputState.ONLINE
        try:
            new_eval_value = eval(
                self.condition,
                {
                    "value": raw_value,
                    "value_float": self.try_float(raw_value),
                    "value_json": self.try_json(raw_value),
                    "is_on": is_on,
                    "is_off": is_off,
                },
            )
        except Exception as e:
            self.service.warning(
                f"Group {self.group}, input {self} | Evaluation of input '{raw_value}' failed: {e}"
            )
            new_eval_value = self.last_eval_value

        self._handle_change(new_eval_value)
        self._store_state()

    @staticmethod
    def try_float(inval):
        try:
            return float(inval)
        except ValueError:
            return float("NaN")

    @staticmethod
    def try_json(inval):
        try:
            return loads(inval)
        except Exception as e:
            return {}

    def __str__(self):
        if not self.format:
            return super().__str__()
        else:
            return f"{self.label} ({self.format})".format(
                value=self.last_raw_value,
                value_float=self.try_float(self.last_raw_value),
            )

    def _check_silence_timeout(self, _):
        self.state = InputState.OFFLINE
        if self.last_update is None:
            assert self.silence_timeout_check_loop is not None
            span = datetime.now() - self.service.started
            self.service.warning(
                f"Group {self.group}, input {self}: Silent since launch ({format_timespan(span)} ago)"
            )
        else:
            span = datetime.now() - self.last_update
            self.service.warning(
                f"Group {self.group}, input {self}: Silent for {format_timespan(span)}"
            )

    def _store_state(self, _=None):
        # service saves periodically
        assert self.service.state
        self.service.state.set_path(
            "mqtt_input",
            self.mqtt,
            self.condition,
            "last_state",
            value={
                "last_raw_value": self.last_raw_value,
                "last_eval_value": self.last_eval_value,
                "last_update": self.last_update,
                "state": self.state.value,
            },
        )

    def _load_state(self):
        assert self.service.state
        stored_state = self.service.state.get_path(
            "mqtt_input", self.mqtt, self.condition, "last_state", default=None
        )
        if stored_state is not None:
            self.last_raw_value = stored_state["last_raw_value"]
            self.last_eval_value = stored_state["last_eval_value"]
            self.last_update = stored_state["last_update"]
            self.state = InputState(stored_state["state"])


class LivenessInput(MQTTInput):
    invalid_response_timeout: timedelta
    invalid_response_timeout_check_loop: miqro.Loop

    def __init__(
        self,
        service,
        group,
        mqtt,
        when,
        label,
        silence_timeout={"hours": 1},
        invalid_response_timeout={"minutes": 3},
    ):
        super().__init__(
            service,
            group,
            mqtt,
            when=when,
            label=label,
            silence_timeout=silence_timeout,
        )
        self.invalid_response_timeout = timedelta(**invalid_response_timeout)
        self.invalid_response_timeout_check_loop = miqro.Loop(
            self.check_invalid_response_timeout, self.invalid_response_timeout, False
        )
        self.service.add_loop(self.invalid_response_timeout_check_loop)

    def handle(self, _, raw_value):
        self.last_update = datetime.now()
        self.last_raw_value = raw_value

        if self.silence_timeout_check_loop:
            self.silence_timeout_check_loop.restart(delayed=True)

        self._handle_change(
            eval(self.condition, {"value": raw_value, "is_on": is_on, "is_off": is_off})
        )

    def check_invalid_response_timeout(self, _):
        self.service.warning(
            f"Group {self.group}, liveness input {self}: Invalid response since {self.last_update}"
        )

    def _handle_change(self, new_eval_value):
        if new_eval_value == self.last_eval_value:
            return False

        self.service.log.info(
            f"Group {self.group}, liveness input {self} | Evaluated value changed to {new_eval_value}"
        )
        self.last_eval_value = new_eval_value

        if new_eval_value:
            self.state = InputState.ONLINE
            self.invalid_response_timeout_check_loop.stop()
        else:
            self.state = InputState.INVALID_RESPONSE
            self.invalid_response_timeout_check_loop.start(delayed=True)

        return True


class AlarmGroup:
    service: "AlarmService"
    name: str
    label: str
    prealarm: Optional[timedelta] = None
    reset_delay: Optional[timedelta] = None
    liveness: List[LivenessInput]
    inputs: List[Union[MQTTInput, MultiInput]]
    inhibitors: List[Union[MQTTInput, MultiInput]]

    text_outputs: Dict[str, List[TextOutput]]
    switch_outputs: Dict[str, List[Tuple[SwitchOutputGroup, str]]]
    priority: int

    state: AlarmState = AlarmState.OFF
    enabled: bool = False
    inhibited_by_command: bool = False

    prealarm_to_alarm_loop: Optional[miqro.Loop] = None
    alarm_to_reset_loop: Optional[miqro.Loop] = None
    inhibit_timeout_loop: miqro.Loop

    def __init__(
        self,
        service,
        priority,
        name,
        label,
        inputs: List[Dict],
        outputs: Dict[str, List[str]],
        prealarm=None,
        reset_delay=None,
        liveness: List[Dict] = [],
        inhibitors: List[Dict] = [],
        default_enabled=False,
    ):
        self.service = service
        self.priority = priority
        self.name = name
        self.label = label

        self.service.log.debug(f"Creating inputs for group {self}")
        self.inputs = Input.create_from_input_list(self.service, self, inputs)
        self.inhibitors = Input.create_from_input_list(self.service, self, inhibitors)
        self.liveness = Input.create_from_liveness_input_list(
            self.service, self, liveness
        )

        self.service.log.debug(f"Assigning outputs for group {self}")
        self.text_outputs = {}
        self.switch_outputs = {}
        for alarm_type, outs in outputs.items():
            self.text_outputs[alarm_type] = []
            self.switch_outputs[alarm_type] = []
            for o in outs:
                if type(o) is dict:
                    # output description is of the form 'output_name: schedule'
                    output_name, schedule = o.popitem()
                    output = self.service.switch_outputs[output_name]
                    self.switch_outputs[alarm_type].append((output, schedule))
                else:
                    output = self.service.text_outputs[o]
                    output.add_group(self)
                    self.text_outputs[alarm_type].append(output)

        assert self.service.state
        self.enabled = self.service.state.get_path(
            "group_enabled", self.name, default=default_enabled
        )

        self.inhibit_timeout_loop = miqro.Loop(
            self.inhibit_timeout, timedelta(minutes=1), False
        )
        self.service.add_loop(self.inhibit_timeout_loop)

        if prealarm:
            self.prealarm = prealarm
            self.prealarm_to_alarm_loop = miqro.Loop(
                self.do_alarm, timedelta(**self.prealarm), False
            )
            self.service.add_loop(self.prealarm_to_alarm_loop)

        if reset_delay:
            self.reset_delay = reset_delay

            self.alarm_to_reset_loop = miqro.Loop(
                self.do_reset, timedelta(**self.reset_delay), False
            )
            self.service.add_loop(self.alarm_to_reset_loop)

        self.service.add_handler(
            self._mqtt_topic("enabled/command"), self.handle_enabled_msg
        )
        self.service.add_handler(
            self._mqtt_topic("inhibited/command"), self.handle_inhibit_msg
        )
        self.service.add_handler(
            self._mqtt_topic("reset/command"), self.handle_reset_msg
        )
        self.service.add_handler(self._mqtt_topic("auto/command"), self.handle_auto_msg)

    def __str__(self):
        return self.label

    # make this class sortable by priority
    def __lt__(self, other):
        return self.priority < other.priority

    def get_active_inputs_string(self):
        return ", ".join(
            [
                str(i)
                for i in self.inputs
                if i.get_state() == InputState.ONLINE and i.get_last_value()
            ]
        )

    def on(self, input):
        self.service.log.info(f" {self} | {input} is on, from state: {self.state}")

        if input in self.inhibitors:
            if self.state == AlarmState.PREALARM:
                self.do_reset(input)
            return

        if self.reset_delay:
            assert self.alarm_to_reset_loop
            self.alarm_to_reset_loop.stop()

        if not self.enabled:
            self.service.log.info(f"{self} is disabled, ignoring")
            return

        if self.inhibited_by_command:
            self.service.log.info(f"{self} is inhibited by command, ignoring")
            return

        if any(i.get_last_value() for i in self.inhibitors):
            self.service.log.info(f"{self} is inhibited by inhibitor, ignoring")
            return

        if self.state == AlarmState.OFF:
            self.do_prealarm(trigger=input)
        elif self.state in (AlarmState.PREALARM, AlarmState.ALARM):
            self.update_outputs()

    def off(self, input):
        if not self.state in [AlarmState.ALARM, AlarmState.PREALARM]:
            return

        self.service.log.info(f" {self} | {input} is off, from state: {self.state}")

        self.update_outputs()

        if not self.reset_delay:  # never reset this alarm automatically
            return

        assert self.alarm_to_reset_loop

        input_states_off_or_invalid = (
            (i.get_state() != InputState.ONLINE or i.get_last_value() == False)
            for i in self.inputs
        )

        if all(input_states_off_or_invalid):
            self.service.log.info(f"Starting timeout for alarm reset")
            self.alarm_to_reset_loop.start(delayed=True)

    def do_prealarm(self, trigger):
        if self.prealarm is None:
            self.do_alarm(trigger)
            return

        self.service.log.info(
            f">> {self} | Prealarm triggered by {type(trigger)} '{trigger}', from state: {self.state}"
        )
        assert self.state != AlarmState.PREALARM

        self.state = AlarmState.PREALARM
        self.update_outputs()
        self.service.request_publish_info()

        if self.prealarm:
            assert self.prealarm_to_alarm_loop
            self.prealarm_to_alarm_loop.start(delayed=True)

    def do_alarm(self, trigger):
        self.service.log.info(
            f">> {self} | Alarm triggered by {type(trigger)} '{trigger}', from state: {self.state}"
        )
        assert self.state != AlarmState.ALARM

        self.state = AlarmState.ALARM
        self.update_outputs()
        self.service.request_publish_info()

        if self.prealarm:
            assert self.prealarm_to_alarm_loop
            self.prealarm_to_alarm_loop.stop()
        return False  # stop the reset loop if triggered from there

    def do_reset(self, trigger):
        self.service.log.info(
            f">> {self} | Reset triggered by {type(trigger)} '{trigger}', from state: {self.state}"
        )
        assert self.state in [AlarmState.ALARM, AlarmState.PREALARM]

        self.state = AlarmState.OFF
        self.reset_outputs()
        self.service.request_publish_info()

        if self.prealarm:
            assert self.prealarm_to_alarm_loop
            self.prealarm_to_alarm_loop.stop()

        if self.reset_delay:  # never reset this alarm automatically
            assert self.alarm_to_reset_loop
            self.alarm_to_reset_loop.stop()

        return False  # stop the reset loop if triggered from there

    def update_outputs(self):
        for output, schedule in self.switch_outputs.get(self.state.name.lower(), []):
            output.request(self, self.state, schedule)

        for output in self.text_outputs.get(self.state.name.lower(), []):
            output.update()

    def reset_outputs(self):
        for _, outputs in self.switch_outputs.items():
            for output, __ in outputs:
                output.request(self, self.state, None)

    def inhibit_timeout(self, _):
        self.inhibited_by_command = False
        self.service.log.info(
            f"{self} | Inhibit by command timeout, state: {self.state}"
        )
        self.service.request_publish_info()
        return False

    def all_ok(self):
        return all(
            i.get_state() == InputState.ONLINE
            for i in self.inputs + self.liveness + self.inhibitors
        )

    def get_state(self):
        self.service.log.info(f"{self} | Assembling state")

        if not self.enabled:
            display_state = "disabled"
        else:
            if self.inhibited_by_command or any(
                i.get_last_value() for i in self.inhibitors
            ):
                display_state = "inhibited"
            else:
                display_state = "enabled"
        if self.state == AlarmState.PREALARM:
            display_state = "prealarm"
        elif self.state == AlarmState.ALARM:
            display_state = "alarm"

        live = all(input.get_state() == InputState.ONLINE for input in self.liveness)
        data = {
            "all_inputs_online": self.all_ok(),
            "enabled/state": self.enabled,
            "inhibited/state": self.inhibited_by_command,
            "any_inhibitor_active": self.inhibited_by_command
            or any(i.get_last_value() for i in self.inhibitors),
            "state": self.state.name.lower(),
            "display_state": display_state,
            "live": live,
            "input": {},
            "inhibitor": {},
            "liveness": {},
            "label": self.label,
        }

        for category, elements in [
            ("input", self.inputs),
            ("inhibitor", self.inhibitors),
            ("liveness", self.liveness),
        ]:
            for input in elements:
                data[category][input.label] = {
                    "state": input.get_state().name.lower(),
                    "value": input.get_last_value(),
                }

        return data

    def handle_enabled_msg(self, _, msg):
        self.set_enabled(is_on(msg))

        if not self.enabled and self.state in [AlarmState.ALARM, AlarmState.PREALARM]:
            self.do_reset("MQTT message, enabled=0")

        if self.enabled:
            self.inhibited_by_command = False

        self.service.log.info(f"{self} | Enabled: {self.enabled}")
        self.service.request_publish_info()

    def handle_inhibit_msg(self, _, msg):
        self.inhibited_by_command = msg.isnumeric() and int(msg) > 0

        if self.inhibited_by_command and self.state == AlarmState.PREALARM:
            self.do_reset("MQTT message, inhibited=1")

        if self.inhibited_by_command:
            self.inhibit_timeout_loop.interval = timedelta(seconds=int(msg))
            self.inhibit_timeout_loop.start(delayed=True)

        self.service.log.info(f"{self} | Inhibited: {self.inhibited_by_command}")
        self.service.request_publish_info()

    def handle_reset_msg(self, _, msg):
        if is_on(msg) and self.state in [AlarmState.ALARM, AlarmState.PREALARM]:
            self.do_reset("MQTT message, reseted=1")

    def handle_auto_msg(self, _, msg):
        """
        Handle auto message. If the alarm is on, reset it. Otherwise, enabled/disable
        this alarm group.
        """
        if not is_on(msg):
            return
        if self.state in [AlarmState.ALARM, AlarmState.PREALARM]:
            self.do_reset("MQTT message, auto=1")
            return

        self.set_enabled(not self.enabled)
        self.service.log.info(f"{self} | Enabled via auto: {self.enabled}")
        self.service.request_publish_info()

    def set_enabled(self, enabled):
        self.enabled = enabled
        # store in service's state
        assert self.service.state
        self.service.state.set_path("group_enabled", self.name, value=enabled)
        self.service.state.save()

    def _mqtt_topic(self, ext):
        return f"{self.name}/{ext}"


class AlarmService(miqro.Service):
    SERVICE_NAME = "alarm"
    USE_STATE_FILE = True

    probe: Optional[SwitchOutput] = None
    text_outputs: Dict[str, TextOutput]
    switch_outputs: Dict[str, SwitchOutputGroup]
    groups: List[AlarmGroup]
    started: datetime

    debug_suppress_info_publish: bool = False
    publish_info_requested: bool = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.started = datetime.now()

        if self.service_config.get("probe", None):
            self.log.debug(f"Creating probe output.")
            self.probe_output = SwitchOutput(self, **self.service_config["probe"])
            self.probe_output.on()

        self.create_outputs()
        self.create_alarm_groups()

    def create_outputs(self):
        self.text_outputs = {}
        for name, config in self.service_config.get("text_outputs", {}).items():
            self.log.debug(f"Creating text output: {name}")
            self.text_outputs[name] = TextOutput(self, **config)

        self.switch_outputs = {}
        for name, config in self.service_config.get("switch_outputs", {}).items():
            self.log.debug(f"Creating switch output: {name}")
            self.switch_outputs[name] = SwitchOutputGroup(self, **config)

    def warning(self, msg):
        self.log.warning(msg)
        for output in self.text_outputs.values():
            if output.info:
                output.send_info(msg)

    def create_alarm_groups(self):
        self.groups = []
        priority = 100
        for config in self.service_config["groups"]:
            priority += 1
            self.log.debug(f"Creating group: {config['name']}")

            if "priority" in config:
                the_priority = config["priority"]
                del config["priority"]
            else:
                the_priority = priority

            self.groups.append(AlarmGroup(self, priority=the_priority, **config))

    @miqro.loop(seconds=180)
    def _publish_info_interval(self):
        if not self.debug_suppress_info_publish:
            self.request_publish_info()

    @miqro.loop(seconds=0.2)
    def _publish_info_on_request(self):
        if self.publish_info_requested:
            self.publish_info()
            self.publish_info_requested = False

    def request_publish_info(self):
        self.publish_info_requested = True

    def publish_info(self):
        data = {group.name: group.get_state() for group in self.groups}
        self.publish_json("info", data, only_if_changed=timedelta(seconds=60))
        self.publish_json_keys(data, only_if_changed=True)

    @miqro.handle("reset/command")
    def handle_reset_command(self, _, msg):
        for group in self.groups:
            group.handle_reset_msg(_, msg)

    @miqro.loop(minutes=5)
    def save_state(self):
        self.state.save()


def run():
    miqro.run(AlarmService)


if __name__ == "__main__":
    run()
