# fusionLogger

A Fusion add-in that records anonymised interaction logs for CAD usability studies. It logs commands,
selections with their geometry, the design environment and timings. It records no names, file paths or account
details. The only free text is what participants type in answer to the per-file questions. Recording starts only after the participant gives consent in the add-in's panel.

It was built for an editability study at the Design and Manufacturing Futures Lab, University of Bristol, and
it can be used for other studies. Checked on Fusion 2705.1.15 (embedded Python 3.14) on macOS.

## Install

1. Download and unzip `fusionLogger-<version>.zip`. Keep the folder name `fusionLogger`.
2. Move the folder into Fusion's add-in folder:
   - macOS: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
   - Windows: `%AppData%\Autodesk\Autodesk Fusion 360\API\AddIns\`
3. In Fusion, open Scripts and Add-Ins (Shift+S), go to Add-Ins, select `fusionLogger` and click Run.
   It then loads with Fusion each time.

Or use the `+` button in Scripts and Add-Ins and pick the folder.

## Run a session

1. The panel opens on the right and the toolbar shows a Start recording button. Nothing is recorded yet.
2. The participant enters their code (such as `P03`), ticks consent and clicks Start recording.
3. A red banner blinks in the panel for as long as recording is on. The panel has no close button. If Fusion
   removes it, for example when the last document closes, it comes back.
4. Every 30 seconds the add-in checks that it is still recording. If a check fails, the banner turns amber,
   says what is wrong and a message box appears once.
5. After editing and saving each file, the participant presses Answer questions about this file and answers two
   short questions: how hard the changes were, from 1 (very easy) to 7 (very hard), and a sentence or two on
   how the model's construction affected them. The button only works once Fusion reports the file as changed, so files that were only
   looked at get no questions. Question time is logged apart from edit time. Files still unanswered are listed at the
   bottom of the panel. Questions can be skipped.
6. To finish, the participant clicks Stop recording. A short checklist appears while recording continues, so
   a last save is still logged. The checklist never blocks stopping. The panel then links the questionnaire,
   if one is set.

Logs go to `~/Documents/FusionLoggerData/`, one file per session, named with the participant code, the UTC
time and a random session id. Stopping and starting again never overwrites an earlier file. On macOS the folder
and files can be read only by the user account that recorded them, and a finished file is read-only. On Windows
they take the permissions of your Documents folder. If that folder can't be written, the panel shows an error and
nothing is recorded.

If Documents syncs to iCloud or OneDrive, the logs, including participants' typed answers, are copied to that
service. Check that your ethics approval allows it, or change `DATA_DIR`. With iCloud, keep the folder downloaded
on that computer.

If Fusion closes without Stop recording, the next launch says that the last recording ended without a clean
stop. It doesn't show whose recording it was, because the next person at the computer may be someone else.

## What is recorded

See `docs/log-schema.md` for every record and field. In short:

- Recorded: commands started and finished, with outcome; selections, as entity type, hashed token and geometry;
  the design environment; saves, opens and closes; timeline size and health; answers to the per-file questions.
- Not recorded: names of any kind, file paths, account details, typed parameter values, cursor and camera
  movement (camera logging is off by default), anything outside Fusion.
- Models and parts are identified by hashes of their names and ids, so the same model keeps one id across sessions.

Two limits to tell your ethics reviewer about:

- **Guessable names.** The name hash has no secret, so anyone who can guess a document's name can confirm it from
  the log. That is harmless for study files, whose names you hand out, but it also applies to any other design a
  participant opens while recording.
- **Other add-ins.** Command ids include those of any other add-ins installed in Fusion, which could hint at a
  participant's employer.

## Adapting it to your study

Edit these before you use it with participants:

| Where | What |
|---|---|
| `palette.html` | The consent text and the list of what is saved. It describes the original study. |
| `fusionLogger.py`, `CONSENT_TEXT_VERSION` | Set to `rec-<your approval reference>` once your wording is approved. The parser warns about logs with any other value. |
| `fusionLogger.py`, `QUESTIONNAIRE_URL` | Your questionnaire link. `{code}` becomes the participant code. Empty means the panel tells people the researcher will hand it over. |
| `fusionLogger.py`, `PARTICIPANT_CODE_RE` | The allowed participant code pattern, `P` and 2 or 3 digits by default. |
| `palette.html` and `fusionLogger.py` | The two per-file questions. |
| `fusionLogger.py`, `DATA_DIR`, `CAMERA_LOGGING`, `HEALTH_INTERVAL_S` | Log folder, camera logging and the check interval. |

Your ethics approval needs to cover what the logger records. `docs/data-durability.md` has a per-session
backup checklist.

## Target checks (researcher use)

The add-in can compare a design with a target shape using volume, surface area, centre of mass and moments of
inertia. It is off for participants (`PARTICIPANT_CHECK = False`). To capture targets, set
`CAPTURE_TARGETS = True`, open each final product named as its model plus `-e1`, click Capture target, then
set it back. Targets are written to `targets.json` next to the add-in.

## Analysis

    python3 analysis/parse_log.py LOGS... --out DIR [--model-map map.json]

This writes `metrics.csv`, one row per session and per model, with the difficulty rating (1 = very easy, 7 = very hard) and question time per model;
`commands.csv`; and the free-text answers, kept apart so you can read them before sharing anything: `notes.csv`
for spreadsheets, where text that could run as a formula gets a leading `'`, and `notes.jsonl` with each answer
exactly as typed. Outputs are readable by their owner only. A log that can't be read is reported and skipped, and
the others are still processed. `map.json` maps real
document names to study codes, such as `{"Bracket baseline": "M00"}`. Keep it with your participant mapping,
not with the logs. Participants must not rename model files or use Save As, because a model's id comes from
its name.

## Tests

    python3 -m unittest discover -s tests

Python 3.12 or later. No Fusion needed: `tests/fake_adsk.py` stands in for the API. When Fusion is installed,
`tests/test_api_surface.py` checks every API member the add-in uses against Fusion's own definitions. Run it
after each Fusion update.

## Licence

MIT. See `LICENSE`.
