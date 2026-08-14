import miqro
import requests
from itertools import count
from typing import Callable, Optional, Dict, List, Tuple, Union
from datetime import timedelta, datetime
from enum import Enum
from dataclasses import dataclass, field
from humanfriendly import format_timespan
from json import loads


# Distinguishes "the configuration did not mention this" from an explicit null,
# which for a timeout means "disable it".
_UNSET = object()


class Severity(Enum):
    """How bad an active condition is."""

    WARNING = "warning"
    ERROR = "error"


@dataclass
class Condition:
    """A problem that is currently true, and was announced when it became true.

    Conditions are what the service tells its users about.  They are announced
    exactly twice -- once when they start and once when they end -- rather than
    every time the code that detects them happens to run, because a channel that
    repeats the same complaint every few seconds is a channel nobody reads.
    """

    severity: Severity
    message: str
    resolved_message: str
    since: datetime


class SwitchOutput:
    service: "AlarmService"
    loop: Optional[miqro.Loop] = None
    message: Optional[str]
    mqtt: Optional[str]
    http_post: Optional[str]

    def __init__(
        self,
        service,
        mqtt=None,
        message=None,
        http_post=None,
        repeat=None,
        health_check=None,
    ):
        if mqtt and not message:
            raise Exception("mqtt is set but message is not")

        self.service = service
        self.mqtt = mqtt
        self.message = message
        self.http_post = http_post
        self.repeat = repeat
        # Optional predicate that must hold for this output to fire.  Used by
        # the liveness probe so that it cannot report health it cannot vouch for.
        self.health_check = health_check

        if self.repeat:
            self.loop = miqro.Loop(self._send, timedelta(**self.repeat), False)
            self.service.add_loop(self.loop)

    def _send(self, _=None):
        if self.health_check is not None and not self.health_check():
            return

        if self.mqtt and self.message:
            # Alarm outputs are worth a redelivery attempt: at QoS 0 a message
            # published during a broker blip is discarded without a trace.
            self.service.publish(self.mqtt, self.message, qos=1, global_=True)
        if self.http_post:
            self._post()

    def _post(self):
        # Short connect timeout: this runs on the loop thread, and every second
        # spent here is a second the prealarm-to-alarm timer does not advance.
        try:
            requests.post(self.http_post, timeout=(3.05, 5))
        except Exception as e:
            self.service.log.error(f"Error posting to {self.http_post}: {e}")
            self.service.note_failure(f"HTTP post to {self.http_post} failed: {e!r}")

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

    def headline(self):
        """How an alarm message announces this state."""
        return {
            AlarmState.OFF: "⎚ Cleared",
            AlarmState.PREALARM: "⚠️ Prealarm",
            AlarmState.ALARM: "🚨 ALARM",
        }[self]


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
    # Assigned by create_outputs once the configured name is known; messages
    # about this output are read by humans, and an object repr is not a name.
    name: str = "unnamed"

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

    def __str__(self):
        return self.name

    def has_output_for(self, schedule: Optional[str], state: AlarmState) -> bool:
        return schedule in self.schedules and state in self.schedules[schedule]

    def _switch_off(self):
        if self.state == AlarmState.OFF:
            return

        # One condition per output for "could not be switched off cleanly": it
        # means a siren may still be running, and it resolves the next time this
        # output switches off the way it is supposed to.
        key = f"output-off:{id(self)}"

        if self.current_schedule is None:
            self.service.raise_condition(
                key,
                f"Output '{self}' | In state {self.state.name.lower()} with no current "
                f"schedule; forcing it to OFF",
                resolved=f"Output '{self}' | Switched off cleanly again",
            )
            self.state = AlarmState.OFF
            return

        output = self.schedules.get(self.current_schedule, {}).get(self.state)
        if output is None:
            self.service.raise_condition(
                key,
                f"Output '{self}' | No '{self.state.name.lower()}' output defined for "
                f"schedule '{self.current_schedule}'; CANNOT SWITCH IT OFF",
                resolved=f"Output '{self}' | Switched off cleanly again",
            )
        else:
            output.off()
            self.service.clear_condition(
                key, f"Output '{self}' | Switched off cleanly again"
            )

        if self.current_schedule in self.resets:
            self.resets[self.current_schedule].on()
        self.state = AlarmState.OFF

    def _switch_on(self, target: AlarmRequest):
        if target.schedule is None:
            self.service.log.error(f"Output '{self}' | Request without a schedule ignored")
            return

        # Keyed by what is missing, so that fixing one schedule resolves its own
        # condition rather than the next alarm's.
        key = f"output-on:{id(self)}:{target.schedule}:{target.state.name}"
        output = self.schedules.get(target.schedule, {}).get(target.state)
        if output is None:
            # Should have been caught by validation at startup; refuse loudly
            # rather than raising out of the middle of the alarm path.
            self.service.raise_condition(
                key,
                f"Output '{self}' | No '{target.state.name.lower()}' output defined for "
                f"schedule '{target.schedule}' -- THIS ALARM PRODUCES NO OUTPUT",
                resolved=f"Output '{self}' | The '{target.state.name.lower()}' output "
                f"for schedule '{target.schedule}' works again",
            )
            return

        self.service.clear_condition(
            key,
            f"Output '{self}' | The '{target.state.name.lower()}' output for schedule "
            f"'{target.schedule}' works again",
        )

        if self.current_schedule and self.current_schedule in self.resets:
            self.resets[self.current_schedule].off()
        output.on()
        self.current_schedule = target.schedule
        self.state = target.state

    def request(self, group, schedule: Optional[str]):
        self.service.log.info(
            f"Output {self} | Request {group.state} for group: {group}"
        )

        # Rebuild rather than deleting in place: the list was maintained as a
        # heap, and removing at an arbitrary index breaks the heap invariant, so
        # requests[0] was no longer necessarily the highest-priority request.
        # Deleting while iterating also skipped entries.
        self.requests = [r for r in self.requests if r.group is not group]

        if group.state != AlarmState.OFF:
            self.requests.append(AlarmRequest(group, group.state, schedule))

        if len(self.requests) == 0:
            self.service.log.info(f"Output {self} | No requests, setting state to OFF")
            self._switch_off()
            return

        target = min(self.requests)

        if self.current_schedule == target.schedule and self.state == target.state:
            self.service.log.info(
                f"Output {self} | Request {group.state.name} for group: {group} "
                f"ignored, already in state: {self.state}"
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
            self.groups.append(group)
            # Sorted, not heapified: the order groups appear in a message should
            # be by priority, and a heap only guarantees its first element.
            self.groups.sort()

    def update(self, update_reason: "UpdateReason"):
        alarm_information = {
            group.label: self._get_group_information(group)
            for group in self.groups
            if group.state in [AlarmState.ALARM, AlarmState.PREALARM]
        }

        self.service.log.info(
            f"TextOutput {self} | Alarm information: {alarm_information}"
        )

        if alarm_information == self.published_alarm_information:
            return

        # Groups that were in the last message and are not in this one have
        # ended.  Whoever was told an alarm started is owed the other half of
        # the story, so they are named rather than silently dropped.
        cleared = [
            label
            for label in (self.published_alarm_information or {})
            if label not in alarm_information
        ]

        message = self._format_msg(alarm_information, update_reason, cleared)

        if not message:
            # Nothing to say: no group is active and none just ended.  Note that
            # publishing the empty string would send an empty SMS through a text
            # gateway.
            self.published_alarm_information = alarm_information
            return

        info = self.service.publish(self.mqtt, message, qos=1, global_=True)

        # Only remember what was published once it actually was: otherwise a
        # failed publish is recorded as delivered and the alarm text is never
        # retried, so the message is lost for good.
        if info is not None and getattr(info, "rc", 0) == 0:
            self.published_alarm_information = alarm_information
        else:
            self.service.log.error(
                f"TextOutput {self} | Publishing alarm text failed; will retry"
            )

    def send_info(self, message):
        # Each notification is sent exactly once, so a redelivery attempt is the
        # difference between the user hearing about a dead sensor and not.
        self.service.publish(self.mqtt, message, qos=1, global_=True)

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

    def _format_msg(self, alarm_information, update_reason, cleared=()):
        output_strings = [
            f"{AlarmState.OFF.headline()}, {label}" for label in cleared
        ]

        for label, g in alarm_information.items():
            # Each line announces its own group's state rather than the event
            # that caused the message.  A message is triggered by one group but
            # restates all of them, so taking the wording from the event marked
            # a group that is merely in prealarm as an ALARM -- and would now
            # put a 🚨 on it.
            headline = AlarmState[g["state"]].headline()
            if update_reason is UpdateReason.UPDATE_ALARM:
                headline += " (update)"
            group_info = f"{headline}, {label}: "
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


class UpdateReason(Enum):
    """Why outputs are being updated.

    Only UPDATE_ALARM changes how a message reads -- the others say which event
    triggered it, which the message no longer takes its wording from, since it
    restates every active group rather than only the one that changed.
    """

    SWITCH_TO_OFF = 0
    SWITCH_TO_PREALARM = 1
    SWITCH_TO_ALARM = 2
    UPDATE_ALARM = 3


class Input:
    service: "AlarmService"
    group: "AlarmGroup"
    label: str

    last_eval_value: Optional[bool] = None
    last_update: Optional[datetime] = None
    state: InputState = InputState.UNKNOWN

    debounce_timeout_check_loop: Optional[miqro.Loop] = None
    debounce_observed_value = None

    # Condition keys have to identify one input object, and labels are only
    # unique within a group -- the same sensor may well appear in two groups.
    _ids = count()

    def __init__(self, service, group, label, debounce=None):
        self.service = service
        self.group = group
        self.label = label
        self._uid = next(Input._ids)

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

    def is_live(self):
        """Whether this input's value can currently be trusted."""
        return self.get_state() == InputState.ONLINE

    def condition_key(self, kind: str) -> str:
        return f"{kind}:{self._uid}:{self.label}"

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

        if not mode in ["and", "or"]:
            raise Exception(
                f"For multi input, mode must be either 'and' or 'or', but not '{mode}'"
            )

        # Set before building the children: each child takes this object as its
        # group, and anything that logs during a child's construction will
        # stringify it.  Assigning afterwards left a half-built parent visible
        # to its own children.
        self.mode = mode
        self.inputs = []
        self.inputs.extend(Input.create_from_input_list(service, self, inputs))

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
        # Tolerate being stringified before construction has finished: this is
        # reached from log messages emitted while the children are being built,
        # and a broken __str__ there turns a log line into a startup crash.
        inputs = getattr(self, "inputs", None)
        mode = getattr(self, "mode", "?")
        count = len(inputs) if inputs is not None else 0
        return f"{self.label} ({count} inputs, '{mode}')"


class MQTTInput(Input):
    mqtt: str
    condition: str
    format: Optional[str]

    # Applied when the configuration does not mention silence_timeout at all.
    # Kept short deliberately: an input that is allowed to stay quiet for days
    # can be dead for days before anyone is told, so a long window has to be an
    # explicit per-input decision.
    DEFAULT_SILENCE_TIMEOUT: Dict = {"hours": 3}

    silence_timeout_check_loop: Optional[miqro.Loop] = None

    last_raw_value: Optional[str] = None
    last_eval_error: Optional[str] = None

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
        silence_timeout: Union[Dict, None, object] = _UNSET,
    ):
        super().__init__(service, group, label, debounce)
        self.mqtt = mqtt
        self.condition = when
        self.format = format

        self.service.add_global_handler(self.mqtt, self.handle)

        if silence_timeout is _UNSET:
            silence_timeout = self.DEFAULT_SILENCE_TIMEOUT

        self.silence_timeout: Optional[timedelta] = (
            timedelta(**silence_timeout) if silence_timeout is not None else None
        )

        if self.silence_timeout is not None:
            self.silence_timeout_check_loop = miqro.Loop(
                self._check_silence_timeout, self.silence_timeout, False
            )
            self.service.add_loop(self.silence_timeout_check_loop)
            self.silence_timeout_check_loop.start(delayed=True)

        self._load_state()

        self.store_state_loop = miqro.Loop(
            self._store_state, timedelta(seconds=30), False
        )
        self.service.add_loop(self.store_state_loop)
        self.store_state_loop.start(delayed=True)

    def _evaluate(self, raw_value):
        """Evaluate the input's condition against a payload.

        Returns ``(ok, value)``.  Never raises: a sensor that starts sending
        payloads the condition cannot handle must not be able to take down the
        service that is watching it.
        """
        try:
            value = eval(
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
            self.last_eval_error = f"{e}"
            self.service.log.warning(
                f"Group {self.group}, input {self} | Evaluation of input "
                f"'{raw_value}' failed: {e}"
            )
            return False, self.last_eval_value

        self.last_eval_error = None
        return True, value

    def handle(self, _, raw_value):
        self.last_update = datetime.now()
        self.last_raw_value = raw_value
        if self.silence_timeout_check_loop:
            self.silence_timeout_check_loop.restart(delayed=True)

        ok, new_eval_value = self._evaluate(raw_value)

        # Only count as online once the payload could actually be interpreted.
        # Marking it online first meant a sensor emitting values the condition
        # cannot handle still reported healthy while being functionally dead.
        self.state = InputState.ONLINE if ok else InputState.INVALID_RESPONSE

        self._report_reception(ok, raw_value)

        self._handle_change(new_eval_value)
        self._store_state()

    def _report_reception(self, ok: bool, raw_value: str):
        """Update the conditions that a received message settles.

        Both are resolved by this message: the input is demonstrably not silent,
        and whether its payload can be understood is known right now.
        """
        self.service.clear_condition(
            self.condition_key("input-silent"),
            f"{self._describe()}: Reporting again",
        )
        self.service.set_condition(
            self.condition_key("input-invalid"),
            not ok,
            message=(
                f"{self._describe()}: Payload '{raw_value}' cannot be evaluated "
                f"({self.last_eval_error}); this input is not protecting anything"
            ),
            resolved=f"{self._describe()}: Payload can be evaluated again",
        )

    def _describe(self) -> str:
        return f"Group {self.group}, input {self}"

    def _go_offline(self, detail: str):
        """Mark the input untrustworthy and say so, once."""
        self.state = InputState.OFFLINE
        self.service.raise_condition(
            self.condition_key("input-silent"),
            f"{self._describe()}: {detail}",
            resolved=f"{self._describe()}: Reporting again",
        )

    @staticmethod
    def try_float(inval):
        try:
            return float(inval)
        except (ValueError, TypeError):
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
        if self.last_update is None:
            span = datetime.now() - self.service.started
            detail = f"Silent since launch ({format_timespan(span)} ago)"
        else:
            span = datetime.now() - self.last_update
            detail = f"Silent for {format_timespan(span)}"

        # This loop keeps running, so the wording stays current for as long as
        # the input is silent -- but only the first pass is announced.
        self._go_offline(detail)

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
        """Restore this input's last reading from the state file.

        Nothing in here may raise: this runs during construction, so a state
        file that is stale, hand-edited, or written by a different version would
        otherwise stop the service from starting at all -- which is a far worse
        outcome than starting with no remembered reading.
        """
        try:
            self._load_state_inner()
        except Exception as e:
            self.service.log.exception(
                f"Input '{self.label}' | Ignoring unusable stored state ({e!r})"
            )
            self.last_raw_value = None
            self.last_eval_value = None
            self.last_update = None
            self.state = InputState.UNKNOWN

    def _load_state_inner(self):
        assert self.service.state
        stored_state = self.service.state.get_path(
            "mqtt_input", self.mqtt, self.condition, "last_state", default=None
        )
        if stored_state is None:
            return

        self.last_raw_value = stored_state["last_raw_value"]
        self.last_eval_value = stored_state["last_eval_value"]
        self.last_update = stored_state["last_update"]
        self.state = InputState(stored_state["state"])

        # Restoring the state avoids reporting every change-only sensor as
        # offline after a restart -- but it must not launder stale data into
        # looking fresh.  If the stored reading is already older than the
        # silence timeout, the input is offline right now, and the timeout check
        # is scheduled for when it actually falls due rather than a full fresh
        # window from boot.
        if self.silence_timeout is None:
            return

        if not isinstance(self.last_update, datetime):
            if self.last_update is not None:
                self.service.log.error(
                    f"Input '{self.label}' | Stored last_update is "
                    f"{self.last_update!r}, not a timestamp; ignoring it"
                )
            self.last_update = None

            # No timestamp means no evidence about *when* this input last
            # reported -- an input that had simply not reported yet when the
            # service was last stopped looks exactly like this.  Claiming it is
            # offline here reports a dead sensor the instant the service starts,
            # before the input has had any chance to say anything, so this run's
            # silence timeout decides instead.
            #
            # Unless it was already offline before the restart: that means a
            # full timeout did elapse without a message, which is evidence, and
            # it is still true now.
            if self.state == InputState.OFFLINE:
                self._go_offline("Has never reported; still offline after a restart")
            else:
                # A stored 'online' with nothing behind it is not evidence
                # either, and must not count as a trustworthy reading.
                self.state = InputState.UNKNOWN
            return

        age = datetime.now() - self.last_update
        if age >= self.silence_timeout:
            self._go_offline(
                f"Silent for {format_timespan(age)}, since before the restart "
                f"(silence timeout {format_timespan(self.silence_timeout)})"
            )
        elif self.silence_timeout_check_loop is not None:
            remaining = max(self.silence_timeout - age, timedelta(seconds=1))
            self.silence_timeout_check_loop.interval = remaining
            self.silence_timeout_check_loop.start(delayed=True)
            self.silence_timeout_check_loop.interval = self.silence_timeout


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
        silence_timeout={"hours": 3},
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

        # A liveness check watches something that may itself be malfunctioning,
        # so an unparseable payload is an expected input here, not a bug.  It
        # counts as an invalid response rather than propagating -- which the
        # grace period below reports, so no 'invalid payload' condition is
        # raised for a liveness input.
        ok, new_eval_value = self._evaluate(raw_value)
        if not ok:
            new_eval_value = False

        self.service.clear_condition(
            self.condition_key("input-silent"),
            f"{self._describe()}: Reporting again",
        )

        self._handle_change(new_eval_value)
        self._store_state()

    def _describe(self) -> str:
        return f"Group {self.group}, liveness input {self}"

    def check_invalid_response_timeout(self, _):
        self.service.raise_condition(
            self.condition_key("liveness-invalid"),
            f"{self._describe()}: Invalid response ({self.last_raw_value}) "
            f"since {self.last_update}",
            resolved=f"{self._describe()}: Responding correctly again",
        )
        return False  # the condition itself keeps this from repeating

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
            self.service.clear_condition(
                self.condition_key("liveness-invalid"),
                f"{self._describe()}: Responding correctly again",
            )
        else:
            self.state = InputState.INVALID_RESPONSE
            # Warn only once the response has been invalid for the configured
            # grace period, so that a single transient bad reading does not page
            # anyone -- this loop was configured but never armed.
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
        self.configured_output_types = list(outputs.keys())
        for alarm_type, outs in outputs.items():
            self.text_outputs[alarm_type] = []
            self.switch_outputs[alarm_type] = []
            for o in outs or []:
                if type(o) is dict:
                    # output description is of the form 'output_name: schedule'
                    # Read without mutating: popitem() empties the dict, and YAML
                    # anchors mean the same dict can be shared between groups.
                    if len(o) != 1:
                        raise Exception(
                            f"Group '{name}': output entry {o!r} must map exactly one "
                            f"output name to one schedule"
                        )
                    output_name, schedule = next(iter(o.items()))
                    if output_name not in self.service.switch_outputs:
                        raise Exception(
                            f"Group '{name}': unknown switch output '{output_name}'"
                        )
                    output = self.service.switch_outputs[output_name]
                    self.switch_outputs[alarm_type].append((output, schedule))
                else:
                    if o not in self.service.text_outputs:
                        raise Exception(f"Group '{name}': unknown text output '{o}'")
                    output = self.service.text_outputs[o]
                    output.add_group(self)
                    self.text_outputs[alarm_type].append(output)

        assert self.service.state
        stored_state = self.service.state.get_path(
            "group_enabled", self.name, default=None
        )
        if stored_state is None:
            self.enabled = default_enabled
            self.service.log.info(
                f"Using default state for group {self}: {self.enabled}"
            )
        else:
            self.enabled = stored_state
            self.service.log.info(
                f"Using stored state for group {self}: {self.enabled}"
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

        self.setup_ha_entities()

    def __str__(self):
        return self.label

    # make this class sortable by priority
    def __lt__(self, other):
        return self.priority < other.priority

    def setup_ha_entities(self):
        self.ha_device = miqro.ha_sensors.Device(
            self.service,
            name=f"Alarm {self.label}",
        )

        ha_enabled_switch = miqro.ha_sensors.Switch(
            device=self.ha_device,
            state_topic_postfix=f"{self.name}/enabled/state",
            command_topic_postfix=f"{self.name}/enabled/command",
            name=f"{self.name} enabled",
            display_name=f"enabled",
        )

        ha_inhibited_switch = miqro.ha_sensors.Switch(
            device=self.ha_device,
            state_topic_postfix=f"{self.name}/inhibited/state",
            command_topic_postfix=f"{self.name}/inhibited/command",
            name=f"{self.name} inhibited",
            display_name=f"inhibited",
        )

        ha_reset_command = miqro.ha_sensors.Button(
            device=self.ha_device,
            command_topic_postfix=f"{self.name}/reset/command",
            name=f"{self.name} reset",
            display_name=f"reset",
        )

        ha_display_state_sensor = miqro.ha_sensors.Sensor(
            device=self.ha_device,
            state_topic_postfix=f"{self.name}/display_state",
            options=["disabled", "enabled", "inhibited", "prealarm", "alarm"],
            name=f"{self.name} display state",
            display_name=f"display state",
        )

        ha_state_sensor = miqro.ha_sensors.Sensor(
            device=self.ha_device,
            state_topic_postfix=f"{self.name}/state",
            options=["off", "prealarm", "alarm"],
            name=f"{self.name} state",
            display_name=f"state",
        )

        ha_liveness_sensor = miqro.ha_sensors.BinarySensor(
            device=self.ha_device,
            state_topic_postfix=f"{self.name}/live",
            name=f"{self.name} all inputs online",
            display_name=f"all inputs online",
        )

        ha_auto_button = miqro.ha_sensors.Button(
            device=self.ha_device,
            command_topic_postfix=f"{self.name}/auto/command",
            name=f"{self.name} on/off/reset",
            display_name=f"on/off/reset",
            json_attributes_topic_postfix=f"{self.name}/info",
        )

        ha_active_inputs_sensor = miqro.ha_sensors.Sensor(
            device=self.ha_device,
            state_topic_postfix=f"{self.name}/active_inputs_text",
            name=f"{self.name} active inputs",
            display_name=f"active inputs",
        )

        ha_offline_inputs_sensor = miqro.ha_sensors.Sensor(
            device=self.ha_device,
            state_topic_postfix=f"{self.name}/offline_inputs_text",
            name=f"{self.name} offline inputs",
            display_name=f"offline inputs",
        )

        ha_active_inhibitors_sensor = miqro.ha_sensors.Sensor(
            device=self.ha_device,
            state_topic_postfix=f"{self.name}/active_inhibitors_text",
            name=f"{self.name} active inhibitors",
            display_name=f"active inhibitors",
        )

        ha_offline_inhibitors_sensor = miqro.ha_sensors.Sensor(
            device=self.ha_device,
            state_topic_postfix=f"{self.name}/offline_inhibitors_text",
            name=f"{self.name} offline inhibitors",
            display_name=f"offline inhibitors",
        )

    @staticmethod
    def _active_string(elements):
        """The elements that are verifiably reading 'triggered' right now.

        Only live elements are listed: the value of an offline input says
        nothing about the world, it is just the last thing that was heard.
        """
        return ", ".join(str(i) for i in elements if i.is_live() and i.get_last_value())

    @staticmethod
    def _offline_string(elements):
        """The elements whose value cannot currently be trusted, with the reason.

        Everything that is not ONLINE is listed -- offline, never seen, and
        unparseable all mean the same thing operationally: this sensor is not
        protecting anything right now.
        """
        return ", ".join(
            f"{i} ({i.get_state().name.lower().replace('_', ' ')})"
            for i in elements
            if not i.is_live()
        )

    def get_active_inputs_string(self):
        return self._active_string(self.inputs)

    def get_offline_inputs_string(self):
        return self._offline_string(self.inputs)

    def get_active_inhibitors_string(self):
        return self._active_string(self.inhibitors)

    def get_offline_inhibitors_string(self):
        return self._offline_string(self.inhibitors)

    def any_inhibitor_active(self):
        """Whether an inhibitor is currently, verifiably suppressing this group.

        Only inhibitors that are live count.  Using the last known value
        regardless of state means an inhibitor that died while reading "true" --
        a presence sensor that lost power, or a value restored from the state
        file after a restart -- suppresses the alarm indefinitely, silently.
        Inhibition fails open.

        Deliberately silent: the fact that an inhibitor is being ignored is
        reported by :meth:`refresh_conditions`, which sees all of them and also
        notices when one becomes trustworthy again.
        """
        return any(
            inhibitor.get_last_value() and inhibitor.is_live()
            for inhibitor in self.inhibitors
        )

    @staticmethod
    def _is_stale(element) -> bool:
        """Reads 'triggered', but that reading can no longer be trusted."""
        return bool(element.get_last_value()) and not element.is_live()

    def refresh_conditions(self):
        """Re-derive the conditions that end without passing through this code.

        An inhibitor that starts reporting again, or an alarm that is reset,
        ends one of these silently, so both directions are decided from the
        current state rather than from the event that caused it.
        """
        for inhibitor in self.inhibitors:
            state = inhibitor.get_state().name.lower().replace("_", " ")
            self.service.set_condition(
                inhibitor.condition_key("stale-inhibitor"),
                self._is_stale(inhibitor),
                severity=Severity.WARNING,
                message=(
                    f"{self} | Inhibitor '{inhibitor}' reads active but is {state}; "
                    f"ignoring it so the alarm stays armed"
                ),
                resolved=f"{self} | Inhibitor '{inhibitor}' counts again ({state})",
            )

        # Only meaningful where an alarm would otherwise reset itself: elsewhere
        # nothing is being held, and the input's own offline condition says all
        # there is to say.
        holds_alarm = self.reset_delay is not None and self.state in (
            AlarmState.ALARM,
            AlarmState.PREALARM,
        )
        for input in self.inputs:
            state = input.get_state().name.lower().replace("_", " ")
            self.service.set_condition(
                input.condition_key("stale-input"),
                holds_alarm and self._is_stale(input),
                severity=Severity.WARNING,
                message=(
                    f"{self} | Input '{input}' went {state} while reading active; "
                    f"holding the alarm rather than resetting it"
                ),
                resolved=f"{self} | Input '{input}' no longer holds the alarm",
            )

    def on(self, input):
        self.update_sensor_stream(input)
        self.service.log.info(f" {self} | {input} is on, from state: {self.state}")

        if input in self.inhibitors:
            if self.state == AlarmState.PREALARM and self.any_inhibitor_active():
                self.do_reset(input)
            return

        if self.reset_delay and self.alarm_to_reset_loop:
            self.alarm_to_reset_loop.stop()

        if not self.enabled:
            self.service.log.info(f"{self} is disabled, ignoring")
            return

        if self.inhibited_by_command:
            self.service.log.info(f"{self} is inhibited by command, ignoring")
            return

        if self.any_inhibitor_active():
            self.service.log.info(f"{self} is inhibited by inhibitor, ignoring")
            return

        if self.state == AlarmState.OFF:
            self.do_prealarm(trigger=input)
        elif self.state in (AlarmState.PREALARM, AlarmState.ALARM):
            self.update_outputs(UpdateReason.UPDATE_ALARM)

    def off(self, input):
        self.update_sensor_stream(input)
        if not self.state in [AlarmState.ALARM, AlarmState.PREALARM]:
            return

        self.service.log.info(f" {self} | {input} is off, from state: {self.state}")

        if not self.reset_delay:  # never reset this alarm automatically
            return

        if not self.alarm_to_reset_loop:
            return

        # An input counts as clear when its last known reading was not
        # triggered.  Previously a *triggered* input that went offline also
        # counted as clear, so an input dying mid-alarm released the alarm --
        # which is exactly what cutting power to a sensor looks like.  An input
        # that has never reported at all still counts as clear, since it is not
        # holding anything.  A triggered-but-dead input holding the alarm is
        # reported by refresh_conditions rather than from here.
        if all(not i.get_last_value() for i in self.inputs):
            self.service.log.info(f"Starting timeout for alarm reset")
            self.alarm_to_reset_loop.start(delayed=True)

    def do_prealarm(self, trigger):
        if self.prealarm is None:
            self.do_alarm(trigger)
            return

        self.service.log.info(
            f">> {self} | Prealarm triggered by {type(trigger)} '{trigger}', from state: {self.state}"
        )
        if self.state == AlarmState.PREALARM:
            self.service.log.warning(f"{self} | Already in prealarm; ignoring")
            return

        self.state = AlarmState.PREALARM
        self._persist_state()
        self.update_outputs(UpdateReason.SWITCH_TO_PREALARM)
        self.service.request_publish_info()

        if self.prealarm_to_alarm_loop:
            self.prealarm_to_alarm_loop.start(delayed=True)

    def do_alarm(self, trigger):
        self.service.log.info(
            f">> {self} | Alarm triggered by {type(trigger)} '{trigger}', from state: {self.state}"
        )
        # Not an assertion: two threads or a timer racing a command must not be
        # able to abort the alarm path, and under `python -O` an assert would
        # vanish and let it continue into a state it was written to reject.
        if self.state == AlarmState.ALARM:
            self.service.log.warning(f"{self} | Already in alarm; ignoring")
            return False

        self.state = AlarmState.ALARM
        self._persist_state()
        self.update_outputs(UpdateReason.SWITCH_TO_ALARM)
        self.service.request_publish_info()

        if self.prealarm_to_alarm_loop:
            self.prealarm_to_alarm_loop.stop()
        return False  # stop the reset loop if triggered from there

    def do_reset(self, trigger):
        self.service.log.info(
            f">> {self} | Reset triggered by {type(trigger)} '{trigger}', from state: {self.state}"
        )
        if self.state not in [AlarmState.ALARM, AlarmState.PREALARM]:
            self.service.log.info(f"{self} | Not active; nothing to reset")
            return False

        self.state = AlarmState.OFF
        self._persist_state()
        self.reset_outputs()
        self.service.request_publish_info()

        if self.prealarm_to_alarm_loop:
            self.prealarm_to_alarm_loop.stop()

        if self.alarm_to_reset_loop:
            self.alarm_to_reset_loop.stop()

        return False  # stop the reset loop if triggered from there

    def _persist_state(self):
        """Record the alarm state so it survives a restart.

        Without this, a crash or a package upgrade during an active alarm turned
        it off silently: the state lived only in memory, no reset was ever sent
        to the outputs, and a still-triggered input would not re-fire because its
        restored value was unchanged.
        """
        assert self.service.state
        self.service.state.set_path("group_state", self.name, value=self.state.name)
        self.service.state.save()

    def _restore_state(self):
        assert self.service.state
        stored = self.service.state.get_path("group_state", self.name, default=None)
        if stored is None:
            return

        try:
            state = AlarmState[stored]
        except KeyError:
            self.service.log.error(f"{self} | Ignoring unknown stored state {stored!r}")
            return

        if state == AlarmState.OFF:
            # The alarm was legitimately off at shutdown. If an input is
            # nevertheless reading active, deliberately do not re-trigger --
            # that is what "reset while the door stayed open" looks like, and
            # firing at boot would be a false alarm. But say so, because
            # otherwise the group looks armed while one of its inputs is already
            # active and cannot rise again.
            active = [
                str(i) for i in self.inputs if i.is_live() and i.get_last_value()
            ]
            if active and self.enabled:
                self.service.notify(
                    f"{self} | Armed after restart while these inputs already read "
                    f"active: {', '.join(active)}. They will not trigger until they "
                    f"clear and become active again."
                )
            return

        self.service.notify(
            f"{self} | Restoring {state.name} state that was active before the "
            f"restart; re-asserting outputs"
        )
        self.state = state
        self.update_outputs(
            UpdateReason.SWITCH_TO_ALARM
            if state == AlarmState.ALARM
            else UpdateReason.SWITCH_TO_PREALARM
        )
        # An interrupted prealarm still has to escalate.
        if state == AlarmState.PREALARM and self.prealarm_to_alarm_loop:
            self.prealarm_to_alarm_loop.start(delayed=True)

    def update_outputs(self, update_reason: UpdateReason):
        for output, schedule in self.switch_outputs.get(self.state.name.lower(), []):
            output.request(self, schedule)

        for output in self.text_outputs.get(self.state.name.lower(), []):
            output.update(update_reason)

    def reset_outputs(self):
        for _, outputs in self.switch_outputs.items():
            for output, __ in outputs:
                output.request(self, None)

        # Reset text outputs: They react only when a *new* input has triggered; so reset after the alarm is required
        for _, outputs in self.text_outputs.items():
            for output in outputs:
                output.update(UpdateReason.SWITCH_TO_OFF)

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
        self.service.log.debug(f"{self} | Assembling state")

        inhibited = self.inhibited_by_command or self.any_inhibitor_active()

        if not self.enabled:
            display_state = "disabled"
        else:
            if inhibited:
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
            "any_inhibitor_active": inhibited,
            "state": self.state.name.lower(),
            "display_state": display_state,
            "live": live,
            "input": {},
            "inhibitor": {},
            "liveness": {},
            "label": self.label,
            "active_inputs_text": self.get_active_inputs_string() or "none",
            "offline_inputs_text": self.get_offline_inputs_string() or "none",
            "active_inhibitors_text": self.get_active_inhibitors_string() or "none",
            "offline_inhibitors_text": self.get_offline_inhibitors_string() or "none",
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

    # Applied when an inhibit command does not name a duration.
    DEFAULT_INHIBIT_TIMEOUT = timedelta(minutes=30)

    def handle_inhibit_msg(self, _, msg):
        """Handle an inhibit command.

        Accepts the same on/off spellings as every other command ("1", "on",
        "true", ...), or a number of seconds to inhibit for.  Previously the
        payload was both at once: "1" meant "inhibit" *and* "for one second", so
        the Home Assistant switch -- which sends "1" -- inhibited the group for
        exactly one second and then silently released it.
        """
        duration = None
        if msg.isnumeric():
            seconds = int(msg)
            inhibit = seconds > 0
            # Treat a bare "1" as a plain "on" rather than a one-second inhibit.
            if seconds > 1:
                duration = timedelta(seconds=min(seconds, 86400))
        else:
            inhibit = is_on(msg)

        self.inhibited_by_command = inhibit

        if inhibit:
            self.inhibit_timeout_loop.interval = duration or self.DEFAULT_INHIBIT_TIMEOUT
            self.inhibit_timeout_loop.start(delayed=True)
            if self.state == AlarmState.PREALARM:
                self.do_reset("MQTT message, inhibited=1")
        else:
            self.inhibit_timeout_loop.stop()

        self.service.log.info(
            f"{self} | Inhibited: {self.inhibited_by_command}"
            + (
                f" for {format_timespan(self.inhibit_timeout_loop.interval)}"
                if inhibit
                else ""
            )
        )
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

    def update_sensor_stream(self, input):
        # input has changed from on to off or vice-versa. now send a nicely formatted
        # message to the sensor stream, i.e., this group's mqtt topic plus 'sensor_stream.
        # the message is formatted like the text message, e.g., "Now: Door open" or "Not any longer: Door open".

        if input.get_last_value():
            message = f"Active: {input.label}"
        else:
            message = f"Inactive: {input.label}"

        self.service.publish(self._mqtt_topic("sensor_stream"), message)

    def _mqtt_topic(self, ext):
        return f"{self.name}/{ext}"


class AlarmService(miqro.Service):
    SERVICE_NAME = "alarm"
    USE_STATE_FILE = True

    # An alarm must not miss an input because the broker connection blinked, so
    # ask the broker to hold messages for us while we are away.
    CLEAN_SESSION = False

    # Every user-facing message starts with one of these, so that the state of
    # the system is readable from the notification itself.
    ICONS = {Severity.ERROR: "🔴", Severity.WARNING: "🟡"}
    ICON_RESOLVED = "🟢"

    # Notifications raised while disconnected wait for the connection; this is
    # how many of them are kept.
    MAX_PENDING_NOTIFICATIONS = 50

    probe_output: Optional[SwitchOutput] = None
    text_outputs: Dict[str, TextOutput]
    switch_outputs: Dict[str, SwitchOutputGroup]
    groups: List[AlarmGroup]
    started: datetime

    debug_suppress_info_publish: bool = False
    publish_info_requested: bool = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.started = datetime.now()
        self._conditions: Dict[str, Condition] = {}
        self._pending_notifications: List[str] = []

        self.create_outputs()
        self.create_alarm_groups()
        self.validate_configuration()
        self.check_input_consistency()

        # Restore alarms that were active before the restart only once
        # everything they drive exists.
        for group in self.groups:
            group._restore_state()

        # Created last: the probe tells an external watchdog that this service
        # is working, so it must not start reporting that until it is.
        if self.service_config.get("probe", None):
            self.log.debug(f"Creating probe output.")
            self.probe_output = SwitchOutput(
                self, **self.service_config["probe"], health_check=self.probe_healthy
            )
            self.probe_output.on()

    def create_outputs(self):
        self.text_outputs = {}
        for name, config in self.service_config.get("text_outputs", {}).items():
            self.log.debug(f"Creating text output: {name}")
            self.text_outputs[name] = TextOutput(self, **config)

        self.switch_outputs = {}
        for name, config in self.service_config.get("switch_outputs", {}).items():
            self.log.debug(f"Creating switch output: {name}")
            # Set after construction rather than as a keyword argument, which
            # would collide with a schedule that happens to be called 'name'.
            output = SwitchOutputGroup(self, **config)
            output.name = name
            self.switch_outputs[name] = output

    def validate_configuration(self):
        """Reject a configuration that cannot drive its outputs.

        Every one of these problems used to surface as a KeyError or an empty
        lookup at the moment an alarm fired -- months after deployment, in the
        one code path nobody wants to debug live -- or not at all.  A startup
        failure is visible; a silent alarm is not.
        """
        problems: List[str] = []

        for group in self.groups:
            for alarm_type in group.configured_output_types:
                if alarm_type not in ("prealarm", "alarm"):
                    problems.append(
                        f"group '{group.name}': unknown output type "
                        f"'{alarm_type}' (expected 'prealarm' or 'alarm')"
                    )

            if group.prealarm and not group.switch_outputs.get("alarm") and not group.text_outputs.get("alarm"):
                problems.append(
                    f"group '{group.name}': has a prealarm but no 'alarm' outputs, "
                    f"so escalating can never do anything"
                )

            if not any(group.switch_outputs.values()) and not any(
                group.text_outputs.values()
            ):
                problems.append(f"group '{group.name}': has no outputs at all")

            if not group.inputs:
                problems.append(f"group '{group.name}': has no inputs")

            for alarm_type, outputs in group.switch_outputs.items():
                if alarm_type not in ("prealarm", "alarm"):
                    continue
                state = (
                    AlarmState.PREALARM if alarm_type == "prealarm" else AlarmState.ALARM
                )
                for output, schedule in outputs:
                    if schedule not in output.schedules:
                        problems.append(
                            f"group '{group.name}' ({alarm_type}): switch output has "
                            f"no schedule '{schedule}'"
                        )
                    elif not output.has_output_for(schedule, state):
                        problems.append(
                            f"group '{group.name}' ({alarm_type}): schedule "
                            f"'{schedule}' defines no '{alarm_type}' message, so this "
                            f"alarm would produce no output"
                        )

        if problems:
            raise Exception(
                "Invalid alarm configuration:\n"
                + "\n".join(f"  - {p}" for p in problems)
            )

        self.log.info(f"Configuration validated: {len(self.groups)} alarm group(s)")

    def _configured_inputs(self):
        """Every MQTT input in the configuration, with the group it serves.

        MultiInput children subscribe to their own topics and run their own
        silence detection, so they count here even though their group only ever
        sees the parent.
        """

        def walk(group, inputs):
            for input in inputs:
                if isinstance(input, MultiInput):
                    yield from walk(group, input.inputs)
                else:
                    yield group, input

        for group in self.groups:
            yield from walk(group, [*group.inputs, *group.inhibitors, *group.liveness])

    @staticmethod
    def _describe_timeout(timeout: Optional[timedelta]) -> str:
        return format_timespan(timeout) if timeout else "no silence detection"

    def check_input_consistency(self):
        """Report inputs that watch the same topic but disagree about it.

        Two inputs on one topic with different silence timeouts describe the
        same sensor as online and offline at the same time, and they share a
        single slot in the state file, so the one that writes last decides what
        the other remembers after a restart.  That is almost always an
        oversight -- but a working alarm built on it must not be refused a
        start, so it is reported rather than rejected.
        """
        by_topic: Dict[str, List[Tuple[AlarmGroup, MQTTInput]]] = {}
        for group, input in self._configured_inputs():
            by_topic.setdefault(input.mqtt, []).append((group, input))

        for topic, users in by_topic.items():
            if len({input.silence_timeout for _, input in users}) < 2:
                continue

            detail = ", ".join(
                f"'{input.label}' in group '{group.name}' "
                f"({self._describe_timeout(input.silence_timeout)})"
                for group, input in users
            )
            self.raise_condition(
                f"topic-timeout-mismatch:{topic}",
                f"Topic '{topic}' is watched by {len(users)} inputs that disagree "
                f"about when it counts as silent: {detail}. The same sensor will be "
                f"reported offline at different times; give them one silence_timeout.",
                severity=Severity.WARNING,
            )

    def probe_healthy(self) -> bool:
        """Whether it is honest to tell an external watchdog that we are fine.

        The probe used to fire unconditionally from the main loop, which proved
        only that the loop was spinning -- it kept reporting healthy with a dead
        MQTT thread or no broker connection at all, i.e. in exactly the states
        where the alarm cannot work.
        """
        if not self.is_connected:
            self.log.error("Probe suppressed: not connected to the broker")
            return False
        if not self.mqtt_thread_alive():
            self.log.critical("Probe suppressed: MQTT network thread is dead")
            return False
        return True

    # -- notifications --------------------------------------------------------

    def condition_counts(self) -> Tuple[int, int]:
        """(errors, warnings) that are active right now."""
        errors = sum(
            1 for c in self._conditions.values() if c.severity is Severity.ERROR
        )
        return errors, len(self._conditions) - errors

    def condition_summary(self) -> str:
        errors, warnings = self.condition_counts()
        return (
            f"{errors} error{'' if errors == 1 else 's'}, "
            f"{warnings} warning{'' if warnings == 1 else 's'}"
        )

    def condition_text(self) -> str:
        """Everything that is currently wrong, for the health topic."""
        if not self._conditions:
            return "none"
        return "; ".join(
            f"{self.ICONS[c.severity]} {c.message} (since {c.since:%Y-%m-%d %H:%M})"
            for c in self._conditions.values()
        )

    def notify(self, message: str, severity: Severity = Severity.WARNING) -> None:
        """Announce a one-off event that has no lasting condition to resolve."""
        self._notify(
            self.ICONS[severity],
            message,
            self.log.error if severity is Severity.ERROR else self.log.warning,
        )

    def set_condition(
        self,
        key: str,
        active: bool,
        *,
        severity: Severity = Severity.ERROR,
        message: str = "",
        resolved: Optional[str] = None,
    ) -> None:
        """Record whether a condition holds, announcing only its transitions.

        Callers may -- and the periodic refresh does -- call this every time they
        re-evaluate the condition; only the first call that turns it on and the
        first one that turns it off produce a message.  While a condition stays
        active its text is kept up to date silently, so the health topic shows
        the current wording (for example a growing "silent for ..." span).
        """
        current = self._conditions.get(key)

        if active:
            if current is not None:
                if message:
                    current.message = message
                return
            self._conditions[key] = Condition(
                severity=severity,
                message=message,
                resolved_message=resolved or f"Resolved: {message}",
                since=datetime.now(),
            )
            self._notify(
                self.ICONS[severity],
                message,
                self.log.error if severity is Severity.ERROR else self.log.warning,
            )
            return

        if current is None:
            return
        del self._conditions[key]
        self._notify(
            self.ICON_RESOLVED, resolved or current.resolved_message, self.log.info
        )

    def raise_condition(
        self,
        key: str,
        message: str,
        severity: Severity = Severity.ERROR,
        resolved: Optional[str] = None,
    ) -> None:
        self.set_condition(
            key, True, severity=severity, message=message, resolved=resolved
        )

    def clear_condition(self, key: str, resolved: Optional[str] = None) -> None:
        self.set_condition(key, False, resolved=resolved)

    def _notify(self, icon: str, message: str, log: Callable[[str], None]) -> None:
        # The counts are baked in here rather than when the message is sent, so
        # that a notification always describes the moment it belongs to.
        text = f"{icon} {message} [{self.condition_summary()}]"
        log(text)

        if not self.is_connected:
            # A publish before the broker connection exists is discarded, and
            # every notification is sent exactly once -- so hold it until there
            # is somewhere for it to go.  This matters at startup, where stored
            # readings are checked for staleness before the first CONNACK.
            # Bounded: an outage lasting days must not grow the process, and the
            # newest messages are the ones worth keeping.
            self._pending_notifications.append(text)
            del self._pending_notifications[: -self.MAX_PENDING_NOTIFICATIONS]
            return

        self._send_notification(text)

    def _send_notification(self, text: str) -> None:
        for output in self.text_outputs.values():
            if output.info:
                output.send_info(text)

    @miqro.loop(seconds=1)
    def _flush_notifications(self):
        if not self._pending_notifications or not self.is_connected:
            return
        pending = self._pending_notifications
        self._pending_notifications = []
        for text in pending:
            self._send_notification(text)

    @miqro.loop(seconds=1)
    def refresh_conditions(self):
        """Re-check the conditions that have no single place where they end.

        An input coming back, an inhibitor becoming trustworthy again or an
        alarm being reset all end one of these without passing through the code
        that noticed them, so they are derived from the current state instead.
        """
        for group in self.groups:
            group.refresh_conditions()

    def create_alarm_groups(self):
        self.groups = []
        priority = 100
        if not self.service_config.get("groups"):
            raise Exception("No alarm groups configured under services.alarm.groups")

        for config in self.service_config["groups"]:
            priority += 1
            self.log.debug(f"Creating group: {config['name']}")

            # Copy rather than deleting the key: the configuration dict belongs
            # to the loaded YAML, and mutating it makes construction
            # non-repeatable.
            config = dict(config)
            the_priority = config.pop("priority", priority)

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
        self.publish_json_keys(data, only_if_changed=True)
        self.publish_json("info", data, only_if_changed=timedelta(seconds=60))
        for group in self.groups:
            data = group.get_state()
            self.publish_json(f"{group.name}/info", data, only_if_changed=timedelta(seconds=30))

    # Note: @miqro.handle registers the undecorated function, so dispatch calls
    # it as handler(service, payload) -- these take (self, msg), unlike the
    # bound methods passed to add_handler, which take (self, _, msg).
    @miqro.handle("reset/command")
    def handle_reset_command(self, msg):
        for group in self.groups:
            group.handle_reset_msg(None, msg)

    @miqro.handle("test/command")
    def handle_test_command(self, msg):
        """Exercise the output path without raising a real alarm.

        Without this, the first proof that the sirens and the text gateway still
        work is a real alarm, which is the worst possible time to discover that
        a topic changed six months ago.
        """
        if not is_on(msg):
            return

        self.log.info("Running output self-test")
        results = []

        for name, output in self.text_outputs.items():
            try:
                output.send_info(
                    f"TEST MESSAGE -- alarm service self-test at "
                    f"{datetime.now():%Y-%m-%d %H:%M:%S}, no action needed"
                )
                results.append(f"{name}: sent")
            except Exception as e:
                results.append(f"{name}: FAILED ({e!r})")
                self.note_failure(f"self-test of text output '{name}' failed: {e!r}")

        summary = "; ".join(results) if results else "no text outputs configured"
        self.log.info(f"Output self-test: {summary}")
        self.publish("test/result", summary)

    @miqro.loop(minutes=5)
    def save_state(self):
        if self.state:
            self.state.save()

    @miqro.loop(minutes=5)
    def report_health(self):
        """Publish whether the service is working, not merely running."""
        errors, warnings = self.condition_counts()
        self.publish("health/ok", self.healthy(), only_if_changed=True)
        self.publish("health/failures", self.failure_count, only_if_changed=True)
        self.publish(
            "health/last_failure", self.last_failure or "none", only_if_changed=True
        )
        # The notifications are one-shot; these say what is wrong *now*, for
        # anything that missed the message or started up afterwards.
        self.publish("health/errors", errors, only_if_changed=True)
        self.publish("health/warnings", warnings, only_if_changed=True)
        self.publish("health/conditions", self.condition_text(), only_if_changed=True)


def run():
    miqro.run(AlarmService)


if __name__ == "__main__":
    run()
