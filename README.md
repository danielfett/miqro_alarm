# miqro_alarm

This is a full-fledged alarm logic, completely based on MQTT inputs and outputs. Reliability was a top priority during development.

Features:

 * Listens to MQTT topics for alarm inputs
 * Custom conditions for alarms can be expressed, e.g., if a value rises above a certain threshold
 * Prealarms are supported - raise awareness to an issue before the full alarm is triggered, or allow time for disabling the alarm
 * Debouncing of input signals is supported, e.g., to avoid false alarms due to sensor noise
 * Alarm outputs:
    * Text messages sent to MQTT topics (e.g., an SMS gateway, push message provider, etc.) 
    * Switch outputs, i.e., MQTT topics expecting custom messages (e.g., to connect to other MQTT services or to drive lights, sirens, etc.)
        * Messages can be repeated in custom intervals
        * Different alarm schedules can be mapped to one topic, e.g., to show a different light pattern for intrusion alarms and fire alarms
 * Alarm groups capture multiple inputs and drive one or more alarm outputs
 * Comprehensive support for liveness checks:
    * If an MQTT topic is not received in a certain interval, send a notification
    * Ensure that other services are alive, send notification if they fail
    * Can call an external HTTP liveness check to ensure the alarm service is still running
 * Support for alarm inhibition (e.g., suppress an intrusion alarm when owner is at home)
 * Alarms can be switched on/off and observed via MQTT

See (examples/miqro.example.yml)[examples/miqro.example.yml] for configuration examples.

## Installing


To install the software, follow these steps **as root**:

 * If `pip3` is not installed, run `apt install python3-pip` first.
 * Then run `pip3 install miqro_alarm` 
 * Create the configuration file `/etc/miqro.yml`
   * See [examples/miqro.example.yml](examples/miqro.example.yml) for an example
   * See [the MIQRO documentation](https://github.com/danielfett/miqro#configuration-file) for other options you can use in the configuration file
 * Install the system service: 
   * `miqro_alarm  --install`
   * `systemctl enable miqro_alarm`
   * `systemctl start miqro_alarm`


## Configuration

The configuration is based on a few concepts:

 * **Outputs** define MQTT topics an alarm can be sent to. This can be:
   * A **text output**: This output is defined by a single MQTT topic that accepts a human-readable message, for example, an SMS gateway.
   * A **switch output**: Defines one or more MQTT topics that accept custom messages. This can be used to drive lights, sirens, etc. You can define separate MQTT topics for different alarm schedules, e.g., to show a different light pattern for intrusion alarms and fire alarms. For each alarm schedule, you can define the MQTT topic, the message that is sent when the alarm is activated, whether the message is to be repeated, and an MQTT topic and message that is sent when the alarm is deactivated.
 * **Alarm Groups** define a set of inputs that are to be monitored. If any of the inputs is triggered, the alarm for the group is activated. Each alarm group offers a number of configuration options:
   * Each group has a **name** that it used in the MQTT messages and a human-readable **label**.
   * The alarm group can be configured to trigger a number of    
     **outputs** at once. 
   * You can also define a **prealarm**, which is activated before the alarm is triggered. After a defined interval, the prealarm is deactivated and the alarm is triggered. 
   * You can define a number of **inputs**. Each input is defined by a single MQTT topic and a condition. If the condition is met, the input is triggered. Conditions can be defined using python expressions. You can define a number of conditions, e.g., if a value rises above a certain threshold or if a value is below a certain threshold. Additionally, there is a timeout for each input that you can define. If no message is received for the input in the defined interval, the input is considered to be dead and a notification will be sent.
   * You can define **liveness checks** for each alarm group: If a liveness check is not triggered in a certain interval, a notification is sent. This can be used, e.g., to check that a sensor sends data in a certain interval or that another service is still running.
   * You can define **inhibitors** for each alarm group. If an inhibitor is triggered, the alarm is inhibited. This can be used, e.g., to suppress an intrusion alarm when the owner is at home.

See (examples/miqro.example.yml)[examples/miqro.example.yml] for configuration examples and explanations.

### Reliability behaviour

Some deliberate choices about what happens when things go wrong:

 * **The configuration is validated at startup.** A group whose outputs reference a
   schedule that does not define a message for the relevant state, an unknown
   output name, a misspelled `prealarm`/`alarm` key, or a group with no inputs or
   no outputs all cause the service to refuse to start. Previously these surfaced
   as an exception at the moment the alarm fired, or not at all.
 * **Inhibition fails open.** An inhibitor only suppresses an alarm while it is
   online. An inhibitor whose last reading was "active" but which has since
   stopped reporting is ignored, and a warning is sent, so a dead presence sensor
   cannot silently disarm the alarm.
 * **Inputs must be understood to count as online.** If an input's `when`
   expression cannot be evaluated for a payload, the input goes to
   `invalid_response` rather than staying `online`, so a sensor emitting
   unexpected values shows up in `all_inputs_online`.
 * **Alarm state survives a restart.** An active alarm or prealarm is persisted
   and re-asserted on startup, including re-sending the outputs and resuming a
   prealarm's escalation timer.
 * **A triggered input that goes offline holds the alarm.** Auto-reset via
   `reset_delay` requires inputs to actually report as clear; an input that stops
   reporting while triggered does not release the alarm.
 * **`silence_timeout` defaults to 6 hours** if not specified. Set it explicitly
   per input, or to `null` to disable silence detection for inputs that only
   report on change.
 * **The `probe` output only fires while the service is healthy.** If the broker
   connection is down or the MQTT thread has died, the probe stops, so an external
   dead-man's switch expires instead of reporting a service that cannot work.

### Optional Homeassistant Integration

When used with Homeassistant, the service publishes entities for every alarm. Each configured alarm is its own device.

#### Example: Interactive Alarm Overview

Automatically generated overview of all alarms. When clicking an active alarm, it is muted. If the alarm is not active, clicking toggles between enabled/disabled.

This requires the HACS plugins [entity-progress-card](https://github.com/francois-le-ko4la/lovelace-entity-progress-card) and [auto-entities](https://github.com/thomasloven/lovelace-auto-entities).

![Homeassistant tiles showing the alarm status](tiles-alarm-status.png)

```yaml
type: custom:auto-entities
filter:
  include:
    - options:
        type: custom:entity-progress-card-template
        icon: >-
          {{ {'enabled': 'mdi:alarm-light-outline', 'disabled':
          'mdi:alarm-light-off-outline', 'prealarm': 'mdi:alarm-light', 'alarm':
          'mdi:alarm-light', 'inhibited':
          'mdi:alarm-light-off'}[state_attr('this.entity_id', 'display_state')]
          }}
        name: "{{device_attr('this.entity_id', 'name')[6:]}}"
        secondary: "{{ state_attr('this.entity_id', 'display_state') }}"
        force_circular_background: true
        color: >-
          {{ {'enabled': 'green', 'disabled': 'grey', 'prealarm': 'orange',
          'alarm': 'red', 'inhibited': 'purple'}[state_attr('this.entity_id',
          'display_state')] }}
        bar_color: red
        bar_effect: shimmer
        hide: progress_bar
        icon_tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: this.entity_id
      device: Alarm*
      entity_id: button.*_on_off_reset
show_empty: true
card:
  type: grid
  square: false
card_param: cards
```

To show only active alarms, add the following (same indentation level as "device").

```yaml
      attributes:
        display_state: alarm
```

#### Example: Overview of Active Inputs

```markdown
type: markdown
content: >-
  {% for x in states | selectattr('entity_id', 'match',
  'sensor.*_active_inputs') %}

  {% if x.state != 'none' %}

  ### {{ x.name }}

  {% for z in x.state.split(', ') %}
    * {{ z }}
  {% endfor %}

  {% endif %}

  {% endfor %}

```

### MQTT Topics

#### :outbox_tray: Published Topics 

**For each alarm group, the following topics are published:**

 * `service/alarm/GROUP1/state` — the state of alarm group GROUP1: `off`, `prealarm`, or `alarm`

 * `service/alarm/GROUP1/enabled/state` — `1` when the group is enabled, `0` otherwise

 * `service/alarm/GROUP1/inhibited/state` — `1` when the alarm is inhibited, `0` otherwise

 * `service/alarm/GROUP1/any_inhibitor_active` — `1` if alarm group is manually inhibited or via a defined inhibitor, `0` otherwise

 * `service/alarm/GROUP1/all_inputs_online` — `1` when all inputs of this group are live, `0` otherwise

 * `service/alarm/GROUP1/live` — `1` when all liveness checks for this group are OK and all inputs and inhibitors are live

 * `service/alarm/g1/display_state` — a unified view on the state of this alarm group: `disabled`, `inhibited`, `enabled`, `prealarm`, `alarm` — recommended for use in user interfaces

**For each input of the group:**

 * `service/alarm/GROUP1/input/INPUT1/state` — the state of this input: `invalid_response`, `unknown`, `offline`, or `online`

 * `service/alarm/GROUP1/input/INPUT1/value` — the last evaluated value for this alarm input — `1` or `0`

Same for each inhibitor (`../inhibitor/..`) and liveness check (`../liveness/..`) of the alarm group.

All outputs listed above are also published in a JSON object at `service/alarm/GROUP1/info`.

**Service health:**

 * `service/alarm/online` — `1` while the service is connected; set to `0` by the broker's last-will if it dies

 * `service/alarm/health/ok` — `1` while the service is connected *and* its MQTT network thread is alive. Prefer this over `online` for monitoring: `online` only proves a process holds the connection.

 * `service/alarm/health/failures` — count of problems that were caught and logged rather than crashing the service (failed publishes, handler exceptions, state save errors). A rising number means the service is degraded even though it is still running.

 * `service/alarm/health/last_failure` — description of the most recent such problem

#### :inbox_tray: Subscribed Topics

The following topics can be used to control the alarm groups:


 * `service/alarm/GROUP1/enabled/command` — send `1` to enable alarm group, `0` to disable

 * `service/alarm/GROUP1/inhibited/command` — send `1` to (manually) inhibit alarm group, `0` to remove manual inhibition

 * `service/alarm/GROUP1/reset/command` — send `1` to reset the alarm when it is active (in `alarm` or `prealarm` state)

 * `service/alarm/GROUP1/auto/command` — send `1` to reset the alarm, if it is in `alarm` or `prealarm` state; otherwise, the alarm is disabled or enabled — this is to be used in user interfaces

 * `service/alarm/reset/command` — send `1` to reset *every* alarm group at once

 * `service/alarm/test/command` — send `1` to run an output self-test: a clearly marked test message is sent to every text output, and the result is published to `service/alarm/test/result`. Use this to verify that notification paths still work without waiting for a real alarm.

Note that `inhibited/command` accepts the same on/off spellings as the other command topics (`1`, `on`, `true`, …), or a number of seconds to inhibit for (`120` inhibits for two minutes). A plain `1` inhibits for the default of 30 minutes.

