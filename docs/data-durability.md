# Data durability protocol

The first dataset was lost, so every session ends with two independent copies off the machine's
single log file. There is no in-app upload. The steps below are the backup.

## Once, before the study

1. In OBS, create a scene that captures the Fusion window at a fixed resolution, recording to MKV so
   a crash leaves a readable file. Save it as a scene collection named `editability`.
2. Create the versioned remote folder. It needs file history and must be covered by the ethics
   approval's data plan.
3. Keep the participant code to person mapping somewhere else entirely. Never in the log folder.

## Every session

Before:
- [ ] Fusion version matches the tagged one in the README. Fusion updates itself.
- [ ] Model files are the supplied ones, and names match the model map.
- [ ] OBS is recording, if the ethics approval allows video.

After:
- [ ] The participant has clicked Stop recording and the banner has gone.
- [ ] Stop the add-in from Scripts and Add-Ins. The dialog shows the log path.
- [ ] Run the parser on the log. It must finish with no unexpected warnings.
- [ ] Copy the log and the recording to remote storage.
- [ ] Check that the remote copy has the same size.
- [ ] Note the session in the run sheet: code, time, anything odd.

Do not delete anything from `~/Documents/FusionLoggerData/` until the study is written up.
A dry run of this list on the self-pilot is part of the sign-off.
