# Author- Peter Rosso  - https://rosso.io/ - Design and Manufacturing Futures Lab https://dmf-lab.co.uk/
# Description-Fusion Logger records anonymised command and selection events for the editability study.
# The button functionality is based on the commandmap add in : https://commandmap.autodesk.com/v1/
import adsk.fusion, adsk.core, traceback, json, os, re, shutil, threading, time, uuid, hashlib, platform, sys, webbrowser
from urllib.parse import quote


_app = adsk.core.Application.cast(None)
_addin = None
_ui = None

handlers = []
_bound = []

_log_file = None
_log_path = None
_session_id = None
_participant_code = None
_review_open = False
_files = {}
_questions_for = None
_targets = None
_seq = 0
_event_counts = {}
_error_count = 0
_last_camera_ms = 0
_bound_names = []
_expected_bindings = 0
_events_since = 0
_checked_errors = 0
_health_failed = []
_health_event = None
_health_handler = None
_health_stop = None
_health_thread = None

SCHEMA_VERSION = 1
ADDIN_VERSION = '0.2.0'
# Stamped into every session so a dataset shows which wording the participant agreed to.
CONSENT_TEXT_VERSION = 'draft-2026-11-07'
# Opened from the palette once recording has stopped. {code} becomes the participant code, so nobody retypes it.
QUESTIONNAIRE_URL = ''
REVIEW_ITEMS = ['edits_attempted', 'models_saved', 'names_kept']
# The author compares saved models with their targets after the session. On for researcher checks only.
PARTICIPANT_CHECK = False
# The two questions after each file. Wording is fixed in palette.html; these are the logged keys.
# 1 = Very easy, 7 = Very hard, so higher means harder, like edit time and errors.
DIFFICULTY_SCALE = range(1, 8)
# Fusion runs these around every tool and on a timer, and the last is this add-in's button. None is an edit.
AMBIENT_COMMANDS = {'SelectCommand', 'CommitCommand', 'AutoSaveFilesCommand', 'PaletteFusionLoggerShow'}
# Researcher only. Keep off for participants: a session refuses to start while it is on.
CAPTURE_TARGETS = False
TARGET_SUFFIX = '-e1'
# Share of each measure's size. Calibrate on the authored models: it must be smaller than every prescribed edit.
DEFAULT_TARGET_TOLERANCE = 0.0005

# Outside the add-in folder so that reinstalling the add-in cannot delete study data.
DATA_DIR = os.path.join(os.path.expanduser('~'), 'Documents', 'FusionLoggerData')
ADDIN_DIR = os.path.dirname(os.path.realpath(__file__))
TARGETS_PATH = os.path.join(ADDIN_DIR, 'targets.json')

# Camera events fire per frame, which lags Fusion and would confound the timing metrics.
CAMERA_LOGGING = False
CAMERA_MIN_INTERVAL_MS = 1000

# A worker thread only queues the check. Fusion runs it on the main thread when idle, so a long command
# delays a check rather than being interrupted by one.
HEALTH_EVENT_ID = 'FusionLoggerHealthCheck'
HEALTH_INTERVAL_S = 30
MIN_FREE_BYTES = 50 * 1024 * 1024
MAX_ERRORS_PER_CHECK = 5
HEALTH_WORDS = {
	'file_open': 'the log file is closed',
	'file_in_place': 'the log file was moved or deleted',
	'write_ok': 'the log file cannot be written',
	'disk_space': 'the disk is almost full',
	'handlers_bound': 'some actions are no longer recorded',
	'errors_steady': 'actions are failing to record',
	'panel_missing': 'the Fusion Logger panel is missing',
	'checks_unavailable': 'the recording checks could not run',
}

PARTICIPANT_CODE_RE = re.compile(r'^P\d{2,3}$')
MAX_SELECTIONS_LOGGED = 50
MAX_TIMELINE_SCAN = 500
PREVIOUS_TAIL_BYTES = 256 * 1024
ANON_PREFIX = 'fusionLogger:'

TERMINATION_REASONS = {0: 'unknown', 1: 'completed', 2: 'cancelled', 3: 'aborted', 4: 'preempted', 5: 'session_ending'}

PALETTE_ID = 'PaletteFusionLogger'
PALETTE_ID_SHOW = 'PaletteFusionLoggerShow'
PALETTE_HTML = os.path.join(ADDIN_DIR, 'palette.html').replace('\\', '/')
# The toolbar button says what pressing it does, so its label and icon also show whether recording is on.
BUTTON_STATES = {
	False: ('Start recording', 'Fusion Logger is not recording. Opens the panel to start.', 'start'),
	True: ('Stop recording', 'Fusion Logger is recording. Opens the checklist to stop.', 'stop'),
}


def bindEventHandler(event, handler):
	event.add(handler)
	handlers.append(handler)

def unbindEventHandler(event, handler):
	event.remove(handler)
	handlers.remove(handler)

def safe(read):
	# One unreadable property must not cost the whole record.
	try:
		return read()
	except:
		return None

def anonId(value):
	if value is None:
		return None
	return hashlib.sha256((ANON_PREFIX + str(value)).encode('utf-8')).hexdigest()[:16]

def baseName(name):
	# Fusion appends " v3" to the name on save; one model must keep one id across versions.
	# The parser's model_hash applies the same rule, so change both together.
	return re.sub(r' v\d+$', '', name)

def modelId(document):
	name = safe(lambda: document.name)
	if name is None:
		return None
	return anonId(baseName(name))

def environmentOf(app, design, editType):
	if editType == 'adsk::fusion::Sketch':
		return 'sketch'
	if design:
		active = safe(lambda: design.activeComponent)
		root = safe(lambda: design.rootComponent)
		if safe(lambda: active.id) != safe(lambda: root.id):
			return 'part'
		return 'assembly' if safe(lambda: root.occurrences.count) else 'part'
	if safe(lambda: app.activeProduct.productType) == 'DrawingProductType':
		return 'drawing'
	return 'other'

def buildContext(document=None):
	app = _app
	design = safe(lambda: adsk.fusion.Design.cast(app.activeProduct))
	editType = safe(lambda: app.activeEditObject.objectType)
	return {
		'model_id': modelId(document or safe(lambda: app.activeDocument)),
		'part_id': anonId(safe(lambda: design.activeComponent.id)),
		'environment': environmentOf(app, design, editType),
		'workspace_id': safe(lambda: _ui.activeWorkspace.id),
		'edit_type': editType,
		'design_type': safe(lambda: design.designType),
	}

def describeEntity(entity):
	# Geometry only. Names are deliberately never read: they can identify people or their work.
	box = safe(lambda: entity.boundingBox)
	return {
		'type': safe(lambda: entity.objectType),
		'token_hash': anonId(safe(lambda: entity.entityToken)),
		'area': safe(lambda: round(entity.area, 4)),
		'length': safe(lambda: round(entity.length, 4)),
		'bbox': safe(lambda: [round(v, 4) for v in box.minPoint.asArray() + box.maxPoint.asArray()]),
	}

def describeSelections(selections):
	described = []
	for selection in list(selections)[:MAX_SELECTIONS_LOGGED]:
		record = describeEntity(selection.entity)
		record['point'] = safe(lambda: [round(v, 4) for v in selection.point.asArray()])
		described.append(record)
	return described

def timelineHealth(scan=True):
	design = safe(lambda: adsk.fusion.Design.cast(_app.activeProduct))
	timeline = safe(lambda: design.timeline)
	count = safe(lambda: timeline.count)
	health = {'timeline_count': count, 'timeline_marker': safe(lambda: timeline.markerPosition),
			  'health_errors': None, 'health_warnings': None}
	if not scan or count is None or count > MAX_TIMELINE_SCAN:
		return health
	errors = warnings = 0
	for i in range(count):
		state = safe(lambda: timeline.item(i).healthState)
		if state == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
			errors += 1
		elif state == adsk.fusion.FeatureHealthStates.WarningFeatureHealthState:
			warnings += 1
	health['health_errors'] = errors
	health['health_warnings'] = warnings
	return health

def buildCommandMap():
	# Built once at session start so that per-event handlers stay cheap on the UI thread.
	commandMap = {}
	def walk(controls, tabId, panelId):
		for i in range(controls.count):
			control = controls.item(i)
			controlId = safe(lambda: control.id)
			if controlId:
				commandMap.setdefault(controlId, []).append([tabId, panelId])
			nested = safe(lambda: control.controls)
			if nested:
				walk(nested, tabId, panelId)
	tabs = _ui.allToolbarTabs
	for t in range(tabs.count):
		tab = tabs.item(t)
		# Some tabs raise RuntimeError here. Losing one must not cost the rest of the map.
		panels = safe(lambda: tab.toolbarPanels)
		if panels is None:
			continue
		for p in range(panels.count):
			panel = panels.item(p)
			walk(panel.controls, tab.id, panel.id)
	return commandMap

def emit(record):
	global _seq
	stamped = {'record': record.pop('record'), 'session_id': _session_id, 'seq': _seq,
			   'ts_ms': round(time.time() * 1000), 'mono_ms': time.monotonic_ns() // 1000000}
	stamped.update(record)
	_seq += 1
	# Flush per event so a Fusion crash loses at most the line being written.
	_log_file.write(json.dumps(stamped, ensure_ascii=False, separators=(',', ':')) + '\n')
	_log_file.flush()

def syncLog():
	_log_file.flush()
	os.fsync(_log_file.fileno())

def recordError(where):
	# Function names and line numbers only: messages and paths can carry usernames and file names.
	global _error_count
	_error_count += 1
	try:
		kind, _, tb = sys.exc_info()
		frames = [[f.name, f.lineno] for f in traceback.extract_tb(tb)]
		emit({'record': 'error', 'where': where, 'error_type': getattr(kind, '__name__', None), 'frames': frames})
	except:
		pass

def captureEvent(name, document=None, **fields):
	global _events_since
	if _log_file is None:
		return
	_events_since += 1
	_event_counts[name] = _event_counts.get(name, 0) + 1
	record = {'record': 'event', 'event': name, 'command_id': None}
	record.update(buildContext(document))
	record.update(fields)
	emit(record)
	if name == 'command_terminated' and record['model_id']:
		noteChange(record['model_id'], document)

def noteChange(modelId, document=None):
	# A file earns its questions once Fusion says it has changed. Starting a command is not enough: orbit, measure
	# and save are commands too, and drags that change the model raise none.
	if modelId and safe(lambda: (document or _app.activeDocument).isModified):
		noteFile(modelId, document)['worked'] = True

def loadLogger():
	bindings = [
		('command_starting', _ui.commandStarting, CommandStartingHandler),
		('command_terminated', _ui.commandTerminated, CommandTerminatedHandler),
		('selection_changed', _ui.activeSelectionChanged, ActiveSelectionChangedHandler),
		('marking_menu', _ui.markingMenuDisplaying, MarkingMenuDisplayingHandler),
		('document_activating', _app.documentActivating, DocumentActivatingHandler),
		('document_saved', _app.documentSaved, DocumentSavedHandler),
		('document_closing', _app.documentClosing, DocumentClosingHandler),
	]
	global _expected_bindings
	if CAMERA_LOGGING:
		bindings.append(('camera_changed', _app.cameraChanged, CameraChangedHandler))
	_expected_bindings = len(bindings)
	names = []
	# Each binding is isolated so that one API change cannot silence the other events.
	for name, event, handlerClass in bindings:
		try:
			handler = handlerClass()
			bindEventHandler(event, handler)
			_bound.append((event, handler))
			names.append(name)
		except:
			recordError('bind:' + name)
	return names

def unloadLogger():
	while _bound:
		event, handler = _bound.pop()
		try:
			unbindEventHandler(event, handler)
		except:
			recordError('unbind')

def startSession(data):
	global _log_file, _log_path, _session_id, _participant_code, _review_open, _targets, _seq, _event_counts, _error_count, _last_camera_ms
	global _bound_names, _events_since, _checked_errors, _health_failed, _files, _questions_for
	code = str(data.get('participant_code', '')).strip().upper()
	if _log_file is not None:
		return sessionStatus(data)
	if not PARTICIPANT_CODE_RE.match(code):
		return {'status': 'error', 'message': 'Enter a participant code like P03.'}
	if data.get('consent') is not True:
		return {'status': 'error', 'message': 'Recording needs your consent.'}
	if CAPTURE_TARGETS:
		return {'status': 'error', 'message': 'Target capture is on. Tell the researcher.'}

	try:
		os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
		# makedirs leaves an existing folder as it was, for example one made by an earlier version.
		safe(lambda: os.chmod(DATA_DIR, 0o700))
		_session_id = uuid.uuid4().hex[:12]
		stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
		_log_path = os.path.join(DATA_DIR, f'{code}_{stamp}_{_session_id}.jsonl')
		_log_file = open(_log_path, 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600))
	except OSError as error:
		_log_file = None
		# No fallback location: a log nobody knows about is how the first dataset was lost.
		return {'status': 'error', 'message': f'Cannot write the log ({error.strerror}). Tell the researcher.'}

	_participant_code = code
	_review_open = False
	_targets, targetsHash = loadTargets()
	_seq = 0
	_event_counts = {}
	_error_count = 0
	_last_camera_ms = 0
	_events_since = 0
	_checked_errors = 0
	_health_failed = []
	_files = {}
	_questions_for = None
	try:
		beginSession(code, targetsHash)
	except:
		# A half-started session would show the banner while nothing is captured or checked, so undo it all.
		abandonSession()
		return {'status': 'error', 'message': 'Cannot write the log. Tell the researcher.'}
	return {'status': 'recording', 'participant_code': code}

def beginSession(code, targetsHash):
	global _bound_names
	emit({'record': 'session_start', 'schema_version': SCHEMA_VERSION, 'addin_version': ADDIN_VERSION,
		  'participant_code': code, 'consent': True, 'consent_text_version': CONSENT_TEXT_VERSION,
		  'fusion_version': safe(lambda: _app.version), 'python_version': platform.python_version(),
		  'os': platform.system(), 'camera_logging': CAMERA_LOGGING,
		  'camera_min_interval_ms': CAMERA_MIN_INTERVAL_MS if CAMERA_LOGGING else None,
		  'targets_sha256': targetsHash, 'health_interval_s': HEALTH_INTERVAL_S,
		  'ambient_commands': sorted(AMBIENT_COMMANDS)})
	try:
		emit({'record': 'command_map', 'commands': buildCommandMap()})
	except:
		recordError('command_map')
	_bound_names = loadLogger()
	emit({'record': 'bindings', 'events': _bound_names})
	captureEvent('logging_started')
	syncLog()
	startHealthChecks()
	updateButton()

def abandonSession():
	global _log_file
	stopHealthChecks()
	unloadLogger()
	safe(lambda: _log_file.close())
	_log_file = None
	updateButton()

def endSession(source='addin_stop', reviewChecked=None):
	global _log_file, _review_open
	if _log_file is None:
		return
	stopHealthChecks()
	closeQuestions('abandoned')
	unloadLogger()
	try:
		# Read-only once finished, so no later script or editor can change a session. The open handle can still write.
		os.fchmod(_log_file.fileno(), 0o400)
	except (OSError, AttributeError):
		# Python before 3.13 has no fchmod on Windows. Setting it by path works there with the file still open.
		try:
			os.chmod(_log_file.name, 0o400)
		except (OSError, TypeError):
			recordError('read_only')
	try:
		emit({'record': 'session_end', 'event_counts': _event_counts, 'error_count': _error_count,
			  'end_source': source, 'review_checked': reviewChecked,
			  'unanswered_files': sum(1 for f in _files.values() if f['worked'] and not f['answered'])})
		syncLog()
	finally:
		# Capture has already stopped, so the session must end even if its last line cannot be written.
		# The parser then reports it as cut short.
		safe(lambda: _log_file.close())
		_log_file = None
		_review_open = False
		updateButton()

def sessionStatus(data):
	if _log_file is None:
		state = {'status': 'idle', 'capture_enabled': True} if CAPTURE_TARGETS else {'status': 'idle'}
		previous = previousSession()
		if previous:
			state['previous'] = previous
		return state
	state = {'status': 'recording', 'participant_code': _participant_code, 'review_open': _review_open,
			 'participant_check': PARTICIPANT_CHECK,
			 # Names go to the panel only, never into the log.
			 'unanswered': [f['name'] for f in _files.values() if f['worked'] and not f['answered']], **healthState()}
	if _questions_for:
		state['questions'] = {'file': _files[_questions_for]['name']}
	return state

def displayName(document):
	name = safe(lambda: document.name)
	return baseName(name) if name else 'this file'

def noteFile(modelId, document=None):
	if modelId not in _files:
		_files[modelId] = {'name': displayName(document or safe(lambda: _app.activeDocument)), 'worked': False, 'answered': False}
	return _files[modelId]

def closeQuestions(outcome):
	global _questions_for
	if _questions_for and _log_file is not None:
		emit({'record': 'file_questions', 'state': 'closed', 'outcome': outcome, 'model_id': _questions_for})
	_questions_for = None

def openQuestions(data):
	global _questions_for
	if _log_file is None:
		return {'status': 'error', 'message': 'Not recording.'}
	document = safe(lambda: _app.activeDocument)
	model = modelId(document)
	if model is None:
		return {'status': 'error', 'message': 'Open the file you have just edited, then press this again.'}
	if not noteFile(model, document)['worked']:
		return {'status': 'error', 'message': 'Make your changes to this file first. The questions come after the edits.'}
	if _questions_for != model:
		closeQuestions('switched')
		# Bound to this file now, so answers cannot land on another file opened while typing.
		_questions_for = model
		emit({'record': 'file_questions', 'state': 'opened', 'model_id': model,
			  'answered_before': _files[model]['answered']})
	return sessionStatus(data)

def saveAnswers(data):
	if _log_file is None or not _questions_for:
		return {'status': 'error', 'message': 'These questions were closed. Press the button again.'}
	difficulty = data.get('difficulty')
	difficulty = difficulty if isinstance(difficulty, int) and not isinstance(difficulty, bool) and difficulty in DIFFICULTY_SCALE else None
	note = data.get('note')
	note = note.strip() if isinstance(note, str) and note.strip() else None
	skipped = [key for key, value in (('difficulty', difficulty), ('note', note)) if value is None]
	emit({'record': 'file_answers', 'model_id': _questions_for, 'difficulty': difficulty, 'note': note, 'skipped': skipped})
	_files[_questions_for]['answered'] = True
	closeQuestions('saved')
	syncLog()
	return sessionStatus(data)

def cancelQuestions(data):
	closeQuestions('cancelled')
	return sessionStatus(data)

def healthState():
	if not _health_failed:
		return {'health': 'ok'}
	words = '; '.join(HEALTH_WORDS.get(name, name) for name in _health_failed)
	return {'health': 'problem', 'health_message': f'Recording problem: {words}. Tell the researcher.'}

def healthTimer(app, stop):
	while not stop.wait(HEALTH_INTERVAL_S):
		try:
			app.fireCustomEvent(HEALTH_EVENT_ID)
		except:
			# A worker thread has nowhere safe to report. A missed check shows as a heartbeat gap in the log.
			pass

def startHealthChecks():
	global _health_event, _health_handler, _health_stop, _health_thread
	try:
		# A crashed run can leave the id registered, and registering it again would fail.
		safe(lambda: _app.unregisterCustomEvent(HEALTH_EVENT_ID))
		_health_event = _app.registerCustomEvent(HEALTH_EVENT_ID)
		_health_handler = HealthCheckHandler()
		bindEventHandler(_health_event, _health_handler)
	except:
		_health_event = _health_handler = None
		recordError('health_start')
		reportHealth(['checks_unavailable'])
		return
	_health_stop = threading.Event()
	_health_thread = threading.Thread(target=healthTimer, args=(_app, _health_stop), name=HEALTH_EVENT_ID, daemon=True)
	_health_thread.start()

def stopHealthChecks():
	global _health_event, _health_handler, _health_stop, _health_thread
	if _health_stop:
		_health_stop.set()
		_health_thread.join(2)
	if _health_handler:
		safe(lambda: unbindEventHandler(_health_event, _health_handler))
		safe(lambda: _app.unregisterCustomEvent(HEALTH_EVENT_ID))
	_health_event = _health_handler = _health_stop = _health_thread = None

def runHealthChecks():
	global _checked_errors, _events_since
	if _log_file is None:
		return None
	failed, restored = [], False
	if _log_file.closed:
		failed.append('file_open')
	else:
		try:
			live, onDisk = os.fstat(_log_file.fileno()), os.stat(_log_path)
			if (live.st_dev, live.st_ino) != (onDisk.st_dev, onDisk.st_ino):
				failed.append('file_in_place')
		except OSError:
			failed.append('file_in_place')
	free = safe(lambda: shutil.disk_usage(DATA_DIR).free)
	if free is not None and free < MIN_FREE_BYTES:
		failed.append('disk_space')
	if len(_bound) < _expected_bindings:
		failed.append('handlers_bound')
	if _error_count - _checked_errors > MAX_ERRORS_PER_CHECK:
		failed.append('errors_steady')
	_checked_errors = _error_count
	# Catches changes made by drags, which raise no command event.
	safe(lambda: noteChange(modelId(_app.activeDocument)))
	# The banner is how participants know they are recorded, so a hidden or deleted panel comes back.
	# Fusion deletes it when the last document closes.
	palette = safe(lambda: _ui.palettes.itemById(PALETTE_ID))
	if palette is None or not palette.isVisible:
		restored = safe(showPalette) is not None
		# With no design open, as in the Open dialog, Fusion may refuse a panel. That is not a recording fault.
		if not restored and safe(lambda: _app.activeDocument) is not None:
			failed.append('panel_missing')
	try:
		before = os.fstat(_log_file.fileno()).st_size
		emit({'record': 'heartbeat', 'checks_failed': failed, 'events_since': _events_since, 'panel_restored': restored})
		syncLog()
		if os.fstat(_log_file.fileno()).st_size <= before:
			failed.append('write_ok')
		_events_since = 0
	except (OSError, ValueError):
		failed.append('write_ok')
	reportHealth(failed)
	return failed

def reportHealth(failed):
	global _health_failed
	added, changed = set(failed) - set(_health_failed), failed != _health_failed
	_health_failed = failed
	if changed:
		palette = safe(lambda: _ui.palettes.itemById(PALETTE_ID))
		if palette:
			safe(lambda: palette.sendInfoToHTML('health', json.dumps(healthState())))
	if added:
		# Interrupts the task once per new problem: losing the data would cost more.
		words = '\n'.join('- ' + HEALTH_WORDS.get(name, name) for name in failed)
		_ui.messageBox('Fusion Logger may not be recording properly:\n' + words + '\n\nTell the researcher before you carry on.')

def setReview(data):
	global _review_open
	isOpen = data.get('open') is True
	if _log_file is not None and isOpen != _review_open:
		if isOpen:
			closeQuestions('stopped')
		_review_open = isOpen
		# Checking saved models can reopen them, so the parser needs to know this is not task time.
		emit({'record': 'review', 'state': 'opened' if isOpen else 'closed'})
	return sessionStatus(data)

def questionnaireUrl():
	return QUESTIONNAIRE_URL.replace('{code}', quote(_participant_code or '')) or None

def finishSession(data):
	if _log_file is None:
		return {'status': 'idle'}
	endSession('palette', [item for item in data.get('checked', []) if item in REVIEW_ITEMS])
	return {'status': 'finished', 'participant_code': _participant_code, 'questionnaire_url': questionnaireUrl()}

def openQuestionnaire(data):
	url = questionnaireUrl()
	if url and webbrowser.open(url):
		return {'status': 'opened'}
	return {'status': 'error', 'message': 'The questionnaire did not open. Tell the researcher.'}

def loadTargets():
	try:
		with open(TARGETS_PATH, 'rb') as f:
			raw = f.read()
		return json.loads(raw), hashlib.sha256(raw).hexdigest()
	except (OSError, ValueError):
		# Every check then reports no target, and the header shows that none was loaded.
		return None, None

def targetFingerprint():
	# Geometry only, so a different construction of the same shape still matches. A boolean against the target
	# would be stricter on paper, but coincident faces are where booleans are least reliable.
	design = adsk.fusion.Design.cast(_app.activeProduct)
	props = design.rootComponent.getPhysicalProperties(adsk.fusion.CalculationAccuracy.VeryHighCalculationAccuracy)
	moments = props.getXYZMomentsOfInertia()[1:]
	return {'volume': props.volume, 'area': props.area, 'center': list(props.centerOfMass.asArray()),
			# Per unit density, so that a change of material is not a change of shape.
			'moments': [m / props.density for m in moments]}

def failedMeasures(current, target, tolerance):
	failed = []
	if abs(current['volume'] - target['volume']) > tolerance * abs(target['volume']):
		failed.append('volume')
	if abs(current['area'] - target['area']) > tolerance * abs(target['area']):
		failed.append('area')
	size = abs(target['volume']) ** (1 / 3)
	if any(abs(a - b) > tolerance * size for a, b in zip(current['center'], target['center'])):
		failed.append('center')
	largest = max(abs(m) for m in target['moments'])
	if any(abs(a - b) > tolerance * largest for a, b in zip(current['moments'], target['moments'])):
		failed.append('moments')
	return failed

def checkTarget(data):
	if _log_file is None:
		return {'status': 'error', 'message': 'Not recording.'}
	if not PARTICIPANT_CHECK:
		return {'status': 'error', 'message': 'Checking is not part of this study.'}
	context = buildContext()
	target = (_targets or {}).get('models', {}).get(context['model_id'])
	result, failed = 'no_target', None
	if target:
		try:
			failed = failedMeasures(targetFingerprint(), target, _targets.get('tolerance', DEFAULT_TARGET_TOLERANCE))
			result = 'no_match' if failed else 'match'
		except:
			recordError('check_target')
			result = 'unreadable'
	_event_counts['target_check'] = _event_counts.get('target_check', 0) + 1
	# Which measures failed, never their values, because a value could reveal what was typed.
	emit({'record': 'target_check', 'result': result, 'failed_measures': failed, 'in_review': _review_open, **context})
	return {'status': 'checked', 'result': result}

def captureTarget(data):
	if not CAPTURE_TARGETS or _log_file is not None:
		return {'status': 'error', 'message': 'Target capture is off.'}
	name = baseName(_app.activeDocument.name)
	if not name.endswith(TARGET_SUFFIX):
		return {'status': 'error', 'message': f'Open the document named <model>{TARGET_SUFFIX}.'}
	targets = loadTargets()[0]
	if targets is None and os.path.exists(TARGETS_PATH):
		# Starting afresh here would silently drop every target captured so far.
		return {'status': 'error', 'message': 'targets.json cannot be read. Fix or move it, then capture again.'}
	targets = targets or {'tolerance': DEFAULT_TARGET_TOLERANCE, 'models': {}}
	targets.setdefault('models', {})[anonId(name[:-len(TARGET_SUFFIX)])] = targetFingerprint()
	partial = TARGETS_PATH + '.partial'
	with open(partial, 'w', encoding='utf-8') as f:
		json.dump(targets, f, indent=1, sort_keys=True)
	os.replace(partial, TARGETS_PATH)
	return {'status': 'captured', 'model': name[:-len(TARGET_SUFFIX)], 'count': len(targets['models'])}

def showPalette():
	palette = _ui.palettes.itemById(PALETTE_ID)
	if palette:
		palette.isVisible = True
		return palette
	# The default browser varies by Fusion release, and consent relies on fusionSendData's promise.
	# No close button: this panel is what tells participants they are being recorded.
	palette = _ui.palettes.add(PALETTE_ID, 'Fusion Logger', PALETTE_HTML, True, False, True, 340, 520, useNewWebBrowser=True)
	if palette is None:
		return None
	palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
	onHTMLEvent = MyHTMLEventHandler()
	palette.incomingFromHTML.add(onHTMLEvent)
	handlers.append(onHTMLEvent)
	onClosed = MyCloseEventHandler()
	palette.closed.add(onClosed)
	handlers.append(onClosed)
	return palette

def updateButton():
	definition = safe(lambda: _ui.commandDefinitions.itemById(PALETTE_ID_SHOW))
	if definition is None:
		return
	name, tooltip, icons = BUTTON_STATES[_log_file is not None]
	# Cosmetic: a failure here must never cost the session.
	safe(lambda: setattr(definition, 'name', name))
	safe(lambda: setattr(definition, 'tooltip', tooltip))
	safe(lambda: setattr(definition, 'resourceFolder', os.path.join(ADDIN_DIR, 'resources', icons)))

def pushState():
	palette = safe(lambda: _ui.palettes.itemById(PALETTE_ID))
	if palette:
		safe(lambda: palette.sendInfoToHTML('state', json.dumps(sessionStatus({}))))

def previousSession():
	# Read from the newest file's last line, so a relaunched Fusion can say how the last recording ended.
	try:
		names = sorted((n for n in os.listdir(DATA_DIR) if n.endswith('.jsonl')),
					   key=lambda n: os.path.getmtime(os.path.join(DATA_DIR, n)))
		if not names:
			return None
		# Nothing is recording, so any file still writable was left by a crash. Lock it like a finished one.
		for name in names:
			safe(lambda: os.chmod(os.path.join(DATA_DIR, name), 0o400))
		with open(os.path.join(DATA_DIR, names[-1]), 'rb') as f:
			f.seek(max(0, os.path.getsize(f.name) - PREVIOUS_TAIL_BYTES))
			lines = f.read().decode('utf-8', 'replace').split('\n')
	except OSError:
		return None
	last, complete = None, True
	for line in reversed([line for line in lines if line.strip()]):
		try:
			last = json.loads(line)
			break
		except ValueError:
			complete = False
	if last is None:
		return None
	if last.get('record') == 'session_end' and last.get('end_source') == 'palette':
		return None
	ended = 'addin_stop' if last.get('record') == 'session_end' and complete else 'cut_short'
	# No participant code: the next person to open Fusion may be someone else, and must not see or reuse it.
	return {'ended': ended,
			'ended_at': time.strftime('%H:%M on %d %b', time.localtime(last.get('ts_ms', 0) / 1000))}

PALETTE_ACTIONS = {'consent': startSession, 'status': sessionStatus, 'review': setReview,
				   'finish': finishSession, 'open_questionnaire': openQuestionnaire,
				   'check_target': checkTarget, 'capture_target': captureTarget,
				   'file_done': openQuestions, 'file_answers': saveAnswers, 'file_cancel': cancelQuestions}


class HealthCheckHandler(adsk.core.CustomEventHandler):
	def __init__(self):
		super().__init__()
	def notify(self, args):
		try:
			runHealthChecks()
		except:
			recordError('health_check')
			reportHealth(['checks_unavailable'])


class CameraChangedHandler(adsk.core.CameraEventHandler):
	def __init__(self):
		super().__init__()
	def notify(self, args):
		global _last_camera_ms
		try:
			now = time.monotonic_ns() // 1000000
			if now - _last_camera_ms < CAMERA_MIN_INTERVAL_MS:
				return
			_last_camera_ms = now
			camera = adsk.core.CameraEventArgs.cast(args).viewport.camera
			captureEvent('camera_changed',
						 view_orientation=safe(lambda: camera.viewOrientation),
						 up_vector=safe(lambda: list(camera.upVector.asArray())),
						 camera_target=safe(lambda: list(camera.target.asArray())))
		except:
			recordError('camera_changed')

class MarkingMenuDisplayingHandler(adsk.core.MarkingMenuEventHandler):
	def __init__(self):
		super().__init__()
	def notify(self, args):
		try:
			entities = list(adsk.core.MarkingMenuEventArgs.cast(args).selectedEntities)
			captureEvent('marking_menu', selection_count=len(entities),
						 selections=[describeEntity(e) for e in entities[:MAX_SELECTIONS_LOGGED]])
		except:
			recordError('marking_menu')

class ActiveSelectionChangedHandler(adsk.core.ActiveSelectionEventHandler):
	def __init__(self):
		super().__init__()
	def notify(self, args):
		try:
			selections = adsk.core.ActiveSelectionEventArgs.cast(args).currentSelection
			captureEvent('selection_changed', selection_count=len(selections),
						 selections=describeSelections(selections))
		except:
			recordError('selection_changed')

class CommandStartingHandler(adsk.core.ApplicationCommandEventHandler):
	def __init__(self):
		super().__init__()
	def notify(self, args):
		try:
			eventArgs = adsk.core.ApplicationCommandEventArgs.cast(args)
			captureEvent('command_starting', command_id=safe(lambda: eventArgs.commandId),
						 is_canceled=safe(lambda: eventArgs.isCanceled))
		except:
			recordError('command_starting')

class CommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
	def __init__(self):
		super().__init__()
	def notify(self, args):
		try:
			eventArgs = adsk.core.ApplicationCommandEventArgs.cast(args)
			reason = safe(lambda: eventArgs.terminationReason)
			commandId = safe(lambda: eventArgs.commandId)
			captureEvent('command_terminated', command_id=commandId,
						 termination_reason=TERMINATION_REASONS.get(reason, reason),
						 **timelineHealth(commandId not in AMBIENT_COMMANDS))
		except:
			recordError('command_terminated')

# documentActivated does not fire on Design and Drawing switches, so task boundaries use Activating.
class DocumentActivatingHandler(adsk.core.DocumentEventHandler):
	def __init__(self):
		super().__init__()
	def notify(self, args):
		try:
			captureEvent('document_activating', document=adsk.core.DocumentEventArgs.cast(args).document)
			if _ui.palettes.itemById(PALETTE_ID) is None:
				showPalette()
			pushState()
		except:
			recordError('document_activating')

class DocumentSavedHandler(adsk.core.DocumentEventHandler):
	def __init__(self):
		super().__init__()
	def notify(self, args):
		try:
			captureEvent('document_saved', document=adsk.core.DocumentEventArgs.cast(args).document)
			syncLog()
		except:
			recordError('document_saved')

class DocumentClosingHandler(adsk.core.DocumentEventHandler):
	def __init__(self):
		super().__init__()
	def notify(self, args):
		try:
			captureEvent('document_closing', document=adsk.core.DocumentEventArgs.cast(args).document)
			syncLog()
			pushState()
		except:
			recordError('document_closing')


class ShowPaletteCommandExecuteHandler(adsk.core.CommandEventHandler):
	def __init__(self):
		super().__init__()

	def notify(self, args):
		try:
			# While recording, the button is Stop recording: it opens the checklist, which still records.
			if _log_file is not None:
				setReview({'open': True})
			if showPalette() is None:
				raise RuntimeError('palette not created')
			pushState()
		except:
			_ui.messageBox('The Fusion Logger panel could not open, so nothing is being recorded.\n\nTell the researcher.')


class ShowPaletteCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
	def __init__(self):
		super().__init__()

	def notify(self, args):
		command = args.command
		onExecute = ShowPaletteCommandExecuteHandler()
		bindEventHandler(command.execute, onExecute)


class MyCloseEventHandler(adsk.core.UserInterfaceGeneralEventHandler):
	def __init__(self):
		super().__init__()

	def notify(self, args):
		if _log_file is not None:
			palette = _ui.palettes.itemById(PALETTE_ID)
			if palette:
				palette.isVisible = True
			_ui.messageBox('Fusion Logger is still recording.\nTo stop, click "Stop recording" in the Fusion Logger panel.')


# The only route that starts recording.
class MyHTMLEventHandler(adsk.core.HTMLEventHandler):
	def __init__(self):
		super().__init__()

	def notify(self, args):
		htmlArgs = adsk.core.HTMLEventArgs.cast(args)
		action = PALETTE_ACTIONS.get(htmlArgs.action)
		if action is None:
			return
		try:
			htmlArgs.returnData = json.dumps(action(json.loads(htmlArgs.data or '{}')))
		except:
			if _log_file is not None:
				recordError(htmlArgs.action)
			htmlArgs.returnData = json.dumps({'status': 'error', 'message': 'That did not work. Tell the researcher.'})


class CommandFusionLoggerAddIn:
	def __init__(self):
		# A definition left by a crashed run has no live handler, so the palette would never open.
		stale = _ui.commandDefinitions.itemById(PALETTE_ID_SHOW)
		if stale:
			stale.deleteMe()
		panel = _ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
		staleControl = panel.controls.itemById(PALETTE_ID_SHOW) if panel else None
		if staleControl:
			staleControl.deleteMe()

		name, tooltip, icons = BUTTON_STATES[False]
		showPaletteCmdDef = _ui.commandDefinitions.addButtonDefinition(PALETTE_ID_SHOW, name, tooltip,
																	   os.path.join(ADDIN_DIR, 'resources', icons))

		# Connect to Command Created event.
		onCommandCreated = ShowPaletteCommandCreatedHandler()
		bindEventHandler(showPaletteCmdDef.commandCreated, onCommandCreated)

		if panel:
			control = panel.controls.addCommand(showPaletteCmdDef)
			# Promoted, so the button sits on the toolbar instead of inside the Add-Ins menu.
			safe(lambda: setattr(control, 'isPromoted', True))

		showPaletteCmdDef.execute()


def run(context):
	global _app, _ui, _addin

	try:
		_app = adsk.core.Application.get()
		_ui = _app.userInterface
		# Nothing is recorded here. Handlers bind only after the palette returns consent.
		_addin = CommandFusionLoggerAddIn()

	except:
		adsk.core.Application.get().userInterface.messageBox('Failed to start the Fusion Logger for Fusion 360 add-in:\n\n{}'.format(traceback.format_exc()))


def stop(context):
	global _addin, _log_path
	ui = adsk.core.Application.get().userInterface
	savedTo = _log_path
	_log_path = None

	try:
		endSession()

		# Delete the palette created by this add-in.
		palette = ui.palettes.itemById(PALETTE_ID)
		if palette:
			palette.deleteMe()

		# Delete controls and associated command definitions created by this add-ins
		panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
		cmd = panel.controls.itemById(PALETTE_ID_SHOW) if panel else None
		if cmd:
			cmd.deleteMe()
		cmdDef = ui.commandDefinitions.itemById(PALETTE_ID_SHOW)
		if cmdDef:
			cmdDef.deleteMe()

		_addin = None
		handlers.clear()

		if savedTo:
			ui.messageBox('Stopped Fusion Logger add-in.\n\nLog saved to:\n' + savedTo)

	except:
		ui.messageBox('Fusion Logger hit a problem while stopping:\n\n{}'.format(traceback.format_exc()))
