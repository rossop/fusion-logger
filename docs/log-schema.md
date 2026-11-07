# Log schema, version 1

One file per session at `~/Documents/FusionLoggerData/<code>_<UTC time>_<session id>.jsonl`, opened in
exclusive mode so no earlier file is ever overwritten. The folder is created readable by its owner only, each
file is owner-only while recording, and a file is made read-only for its owner on a clean end.
Every line is one JSON object. Fields are always present on their record type and are `null` when
Fusion could not supply them.

Every record has: `record`, `session_id`, `seq` (from 0, no gaps), `ts_ms` (host clock, epoch ms),
`mono_ms` (monotonic clock, used to detect clock jumps).

## Records, in file order

| record | when | extra fields |
|---|---|---|
| `session_start` | first line | `schema_version`, `addin_version`, `participant_code`, `consent`, `consent_text_version`, `fusion_version`, `python_version`, `os`, `camera_logging`, `camera_min_interval_ms`, `targets_sha256`, `health_interval_s`, `ambient_commands` (the commands Fusion runs by itself, left out of command counts) |
| `command_map` | once | `commands`: control id to `[[tab id, panel id], ...]` |
| `bindings` | once | `events`: names that bound. A missing name means that source failed |
| `event` | each action | see below |
| `error` | capture failure | `where`, `error_type`, `frames` (`[function, line]` pairs) |
| `review` | participant opens or leaves the end checklist | `state`: `opened` or `closed` |
| `target_check` | Check this model is pressed. Researcher use only: off for participants (`PARTICIPANT_CHECK`) | `result` (`match`, `no_match`, `no_target`, `unreadable`), `failed_measures` (names only, `null` without a comparison), `in_review`, and the event context fields |
| `heartbeat` | each health check, every `health_interval_s` while Fusion is idle | `checks_failed` (names, empty when healthy: `file_open`, `file_in_place`, `write_ok`, `disk_space`, `handlers_bound`, `errors_steady`, `panel_missing`), `events_since` (events since the last heartbeat), `panel_restored` |
| `file_questions` | participant opens or leaves the two questions for a file | `state` (`opened`, `closed`), `model_id` (bound when opened), `answered_before` on open, `outcome` on close (`saved`, `cancelled`, `switched`, `stopped`, `abandoned`). Events inside the window are not task time |
| `file_answers` | participant saves the answers | `model_id`, `difficulty` (1 = very easy to 7 = very hard, `null` if skipped. Pre-release logs may have `ease` instead, where 7 = very easy; the parser warns and leaves those out), `note` (free text as typed, `null` if blank), `skipped` (item names) |
| `session_end` | last line on a clean stop | `event_counts`, `error_count`, `end_source` (`palette` or `addin_stop`), `review_checked` (ticked checklist ids, `null` on an add-in stop), `unanswered_files` (edited files with no saved answers. A file counts as edited once Fusion reports it modified, after a command or at a health check) |

## Event fields

All events: `event`, `command_id`, `model_id`, `part_id`, `environment`, `workspace_id`, `edit_type`,
`design_type`.

- `model_id`: hash of the document name without its ` vN` suffix. Stable across versions and copies.
- `part_id`: hash of the active component's id. This is the part scope for task switching.
- `environment`: `sketch`, `part`, `assembly`, `drawing` or `other`. `assembly` means the root
  component is active and has occurrences. A sub-component being edited counts as `part`.

| event | extra fields |
|---|---|
| `logging_started` | marks consent. Not task time |
| `command_starting` | `is_canceled` |
| `command_terminated` | `termination_reason` (`completed`, `cancelled`, `aborted`, `preempted`, `session_ending`, `unknown`), `timeline_count`, `timeline_marker`, `health_errors`, `health_warnings`. The health counts are `null` after the ambient commands, which run around every tool, and on timelines over 500 items |
| `selection_changed` | `selection_count`, `selections` |
| `marking_menu` | `selection_count`, `selections` (no `point`) |
| `document_activating`, `document_saved`, `document_closing` | none |
| `camera_changed` | only with `CAMERA_LOGGING`: `view_orientation`, `up_vector`, `camera_target` |

A selection is `type`, `token_hash`, `area`, `length`, `bbox`, `point`. Lengths are in Fusion's internal
centimetres. At most 50 are listed per event; `selection_count` is always the true count.

## Capture boundary

The only free text in the log is the participant's answer to the second file question, kept as typed. It can
contain anything they write, so read it before any sharing.

Not captured, on purpose: names of any kind, file paths, account details, typed parameter values,
cursor movement, camera movement (by default), anything outside Fusion.

Not capturable: some direct-manipulation drags raise no command event. There is no global
`commandExecuted` event, so outcome comes from `commandTerminated`. Command dialog inputs are readable
only for an add-in's own commands, so edits to native commands are not recorded. Picks made inside a native
tool (the profile for an extrude, the edges for a fillet) raise no `selection_changed` with content: in the
first two real sessions, no pick was logged during any of more than 40 tool runs.

A model's id comes from its name, so a design's id changes when an untitled design is first saved or a
model is renamed, and all untitled designs share one id.

## Metrics

`analysis/parse_log.py` writes `metrics.csv` (one row per session and per model), `commands.csv` and
`notes.csv` and `notes.jsonl` (the free-text answers, kept apart so they can be read before anything is shared;
the `.jsonl` copy keeps each note exactly as typed). All outputs are readable by their owner only. Events inside
the end checklist or a file's questions are left out, because checking a saved model can open it again. Any
command run in those windows raises a warning, since it may be a real edit. The first event after such a window
starts a new visit, so the window's time is never counted as part of a model's time. Cells starting with `=`,
`+`, `-` or `@` get a leading `'`, so a spreadsheet does not run them as formulas.

- **Completion time:** first to last event of each visit to a model, summed over visits. Pauses count.
- **Active time:** the same, leaving out gaps over 60 s.
- **Task switching:** events whose `part_id` differs from the previous event with a part, divided by
  active minutes.
- **Commands:** `command_starting` events, leaving out the header's `ambient_commands` (`SelectCommand`,
  `CommitCommand`, `AutoSaveFilesCommand` and the logger's own button). Older logs without the field use the same
  set. `commands.csv` keeps them. Orbit and pan count.
- **Selections:** each `selection_changed` carries the whole current selection, so only entities added since the
  previous event count as picks. A repeat is a pick of an entity picked before in the same scope.
- **Cancelled commands:** `cancelled` or `aborted`, with the same exclusions. `preempted` is not a cancel:
  it means another command took over, which is how Fusion normally leaves a tool.
- **Error commands:** command ids containing `delete`, `undo` or `abort`. A real log confirms the ids
  `FusionDeleteCommand` and `UndoCommand`; no id containing `abort` has been seen.
- **New health errors:** rises in `health_errors` between terminated commands on the same model.
- **Answers:** per model, how many times the questions were saved, the latest `difficulty`, and `questions_time_ms`,
  the time the questions were open. The latest note goes to `notes.csv`.

- **Target checks:** presses per model, plus whether the first and last decided results matched. The first shows
  whether the participant judged the model finished correctly unaided. Presses do not change a model's time.

Success is coded by hand and is not derived from logs.
