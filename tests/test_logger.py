import glob, importlib, json, os, sys, tempfile, threading, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import fake_adsk

PII = ['SECRET-USERNAME', 'SECRET-USERID', 'SECRET-DOCUMENT', 'SECRET-EDIT-OBJECT', 'SECRET-FEATURE-NAME', 'secret-token']


class LoggerBase(unittest.TestCase):
    def setUp(self):
        self.app = fake_adsk.install()
        sys.modules.pop('fusionLogger', None)
        self.logger = importlib.import_module('fusionLogger')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.logger.endSession)
        self.logger.DATA_DIR = os.path.join(self.tmp.name, 'nested', 'FusionLoggerData')
        self.logger.TARGETS_PATH = os.path.join(self.tmp.name, 'targets.json')
        self.logger.run(None)
        self.ui = self.app.userInterface
        self.palette = self.ui.palettes.itemById(self.logger.PALETTE_ID)

    def consent(self, code='P03', agreed=True):
        args = fake_adsk.Args(action='consent', data=json.dumps({'participant_code': code, 'consent': agreed}), returnData='')
        self.palette.incomingFromHTML.fire(args)
        return json.loads(args.returnData)

    def send(self, action, **data):
        args = fake_adsk.Args(action=action, data=json.dumps(data), returnData='')
        self.palette.incomingFromHTML.fire(args)
        return json.loads(args.returnData)

    def logs(self):
        return glob.glob(os.path.join(self.logger.DATA_DIR, '*'))

    def records(self):
        with open(self.logs()[0], encoding='utf-8') as f:
            return [json.loads(line) for line in f]

    def events(self, name):
        return [r for r in self.records() if r.get('event') == name]


class LoggerCase(LoggerBase):
    def test_palette_uses_the_browser_consent_relies_on(self):
        self.assertTrue(self.palette.uses_new_browser)
        self.assertTrue(os.path.isabs(self.palette.htmlFileURL))
        self.assertTrue(os.path.exists(self.palette.htmlFileURL))

    def test_no_handlers_or_file_before_consent(self):
        self.assertEqual(self.ui.commandStarting.listeners, [])
        self.assertEqual(self.app.documentSaved.listeners, [])
        self.assertFalse(os.path.exists(self.logger.DATA_DIR))
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='x'))

    def test_refused_consent_or_bad_code_records_nothing(self):
        self.assertEqual(self.consent(agreed=False)['status'], 'error')
        self.assertEqual(self.consent(code='Peter Rosso')['status'], 'error')
        self.assertEqual(self.ui.commandStarting.listeners, [])
        self.assertFalse(os.path.exists(self.logger.DATA_DIR))

    def test_second_consent_keeps_the_same_session(self):
        self.consent(code='p03')
        self.assertEqual(self.consent(code='p03')['participant_code'], 'P03')

    def test_consent_with_another_code_mid_session_reports_the_running_session(self):
        self.consent(code='P03')
        reply = self.consent(code='P05')
        self.assertEqual((reply['status'], reply['participant_code'], reply['health']), ('recording', 'P03', 'ok'))
        self.assertEqual(len(self.logs()), 1)
        self.assertEqual(len(self.logs()), 1)
        self.assertEqual(len(self.ui.commandStarting.listeners), 1)

    def test_consent_starts_session_in_durable_directory(self):
        self.assertEqual(self.consent(code=' p03 '), {'status': 'recording', 'participant_code': 'P03'})
        self.assertEqual(len(self.ui.commandStarting.listeners), 1)
        self.assertEqual(len(self.logs()), 1)
        header = self.records()[0]
        self.assertEqual(header['record'], 'session_start')
        self.assertIs(header['consent'], True)
        self.assertEqual(header['participant_code'], 'P03')
        self.assertEqual(header['fusion_version'], '2705.1.15')

    def test_default_data_dir_is_outside_addin(self):
        sys.modules.pop('fusionLogger')
        fresh = importlib.import_module('fusionLogger')
        self.assertFalse(fresh.DATA_DIR.startswith(fresh.ADDIN_DIR))

    def test_unwritable_directory_refuses_to_record(self):
        blocker = os.path.join(self.tmp.name, 'file')
        open(blocker, 'w').close()
        self.logger.DATA_DIR = os.path.join(blocker, 'sub')
        self.assertEqual(self.consent()['status'], 'error')
        self.assertEqual(self.ui.commandStarting.listeners, [])

    def test_no_pii_in_lines_or_filename(self):
        self.consent()
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='ExtrudeCommand', isCanceled=False))
        self.ui.activeSelectionChanged.fire(fake_adsk.Args(currentSelection=[fake_adsk.Selection()]))
        self.ui.markingMenuDisplaying.fire(fake_adsk.Args(selectedEntities=[fake_adsk.Face()]))
        self.app.documentSaved.fire(fake_adsk.Args(document=self.app.activeDocument))
        self.logger.stop(None)
        path = self.logs()[0]
        with open(path, encoding='utf-8') as f:
            text = f.read()
        for secret in PII:
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, path)
        self.assertNotIn(os.path.expanduser('~'), text)

    def test_every_line_parses_despite_awkward_document_name(self):
        self.consent()
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='A|B', isCanceled=False))
        self.assertEqual([r['seq'] for r in self.records()], list(range(len(self.records()))))

    def test_event_line_has_fixed_fields(self):
        self.consent()
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='ExtrudeCommand', isCanceled=False))
        event = self.events('command_starting')[0]
        for field in ['session_id', 'seq', 'ts_ms', 'mono_ms', 'event', 'command_id', 'model_id', 'part_id', 'environment']:
            self.assertIn(field, event)
        self.assertIsInstance(event['ts_ms'], int)
        self.assertEqual(event['command_id'], 'ExtrudeCommand')

    def test_version_suffix_keeps_model_id(self):
        first = self.logger.modelId(fake_adsk.Args(name='Bracket v1'))
        self.assertEqual(first, self.logger.modelId(fake_adsk.Args(name='Bracket v12')))
        self.assertNotEqual(first, self.logger.modelId(fake_adsk.Args(name='Housing v1')))

    def test_selection_is_a_list_not_a_vector(self):
        self.consent()
        self.ui.activeSelectionChanged.fire(fake_adsk.Args(currentSelection=[fake_adsk.Selection()]))
        event = self.events('selection_changed')[0]
        self.assertEqual(event['selection_count'], 1)
        selection = event['selections'][0]
        self.assertEqual(selection['type'], 'adsk::fusion::BRepFace')
        self.assertEqual(selection['area'], 12.3457)
        self.assertIsNone(selection['length'])
        self.assertEqual(selection['point'], [1.0, 2.0, 3.0])
        self.assertEqual(selection['bbox'], [0.0, 0.0, 0.0, 1.2346, 2.0, 3.0])

    def test_large_selection_is_capped_but_counted(self):
        self.consent()
        many = [fake_adsk.Selection() for _ in range(self.logger.MAX_SELECTIONS_LOGGED + 5)]
        self.ui.activeSelectionChanged.fire(fake_adsk.Args(currentSelection=many))
        event = self.events('selection_changed')[0]
        self.assertEqual(event['selection_count'], self.logger.MAX_SELECTIONS_LOGGED + 5)
        self.assertEqual(len(event['selections']), self.logger.MAX_SELECTIONS_LOGGED)

    def test_preempted_termination_is_named(self):
        self.consent()
        self.ui.commandTerminated.fire(fake_adsk.Args(commandId='SketchDimension', terminationReason=4))
        self.assertEqual(self.events('command_terminated')[0]['termination_reason'], 'preempted')

    def test_termination_records_outcome_and_health(self):
        self.consent()
        self.ui.commandTerminated.fire(fake_adsk.Args(commandId='ExtrudeCommand', terminationReason=2))
        event = self.events('command_terminated')[0]
        self.assertEqual(event['termination_reason'], 'cancelled')
        self.assertEqual((event['timeline_count'], event['health_warnings'], event['health_errors']), (3, 1, 1))

    def test_select_skips_the_timeline_scan(self):
        self.consent()
        self.ui.commandTerminated.fire(fake_adsk.Args(commandId='SelectCommand', terminationReason=4))
        event = self.events('command_terminated')[0]
        self.assertEqual((event['timeline_count'], event['health_errors']), (3, None))

    @unittest.skipIf(os.name == 'nt', 'Windows has a read-only flag, not owner permissions.')
    def test_log_folder_and_file_are_owner_only(self):
        self.consent()
        self.assertEqual(os.stat(self.logger.DATA_DIR).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.logs()[0]).st_mode & 0o777, 0o600)
        self.send('finish')
        self.assertEqual(os.stat(self.logs()[0]).st_mode & 0o777, 0o400)

    def test_environment_scopes(self):
        self.consent()
        design = self.app.activeProduct
        self.assertEqual(self.logger.buildContext()['environment'], 'part')
        design.rootComponent.occurrences.count = 2
        self.assertEqual(self.logger.buildContext()['environment'], 'assembly')
        design.activeComponent = fake_adsk.Component('child')
        self.assertEqual(self.logger.buildContext()['environment'], 'part')
        self.app.activeEditObject = fake_adsk.Args(objectType='adsk::fusion::Sketch')
        self.assertEqual(self.logger.buildContext()['environment'], 'sketch')
        self.app.activeEditObject = None
        self.app.activeProduct = fake_adsk.Args(productType='DrawingProductType')
        self.assertEqual(self.logger.buildContext()['environment'], 'drawing')

    def test_part_id_changes_with_active_component(self):
        self.consent()
        before = self.logger.buildContext()['part_id']
        self.app.activeProduct.activeComponent = fake_adsk.Component('child')
        self.assertNotEqual(before, self.logger.buildContext()['part_id'])

    def test_unreadable_property_keeps_the_event(self):
        self.consent()
        del self.app.activeEditObject
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='ExtrudeCommand', isCanceled=False))
        self.assertEqual(self.events('command_starting')[0]['edit_type'], None)

    def test_handler_failure_is_recorded_without_message_or_path(self):
        self.consent()
        self.ui.activeSelectionChanged.fire(fake_adsk.Args())
        errors = [r for r in self.records() if r['record'] == 'error']
        self.assertEqual(errors[0]['error_type'], 'AttributeError')
        self.assertEqual(sorted(errors[0]), ['error_type', 'frames', 'mono_ms', 'record', 'seq', 'session_id', 'ts_ms', 'where'])
        self.assertTrue(all(isinstance(name, str) and os.sep not in name for name, _ in errors[0]['frames']))
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='still-logging', isCanceled=False))
        self.assertEqual(len(self.events('command_starting')), 1)

    def test_camera_off_by_default(self):
        self.consent()
        self.assertEqual(self.app.cameraChanged.listeners, [])

    def test_camera_flag_throttles(self):
        self.logger.CAMERA_LOGGING = True
        self.consent()
        camera = fake_adsk.Args(viewOrientation=1, upVector=fake_adsk.Point(0, 0, 1), target=fake_adsk.Point(0, 0, 0))
        for _ in range(100):
            self.app.cameraChanged.fire(fake_adsk.Args(viewport=fake_adsk.Args(camera=camera)))
        self.assertEqual(len(self.events('camera_changed')), 1)

    def test_events_on_disk_before_stop(self):
        self.consent()
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='ExtrudeCommand', isCanceled=False))
        self.assertEqual(len(self.events('command_starting')), 1)

    def test_stop_ends_session_and_cleans_up(self):
        self.consent()
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='ExtrudeCommand', isCanceled=False))
        self.logger.stop(None)
        last = self.records()[-1]
        self.assertEqual(last['record'], 'session_end')
        self.assertEqual(last['event_counts']['command_starting'], 1)
        self.assertEqual(self.ui.commandStarting.listeners, [])
        self.assertIsNone(self.ui.palettes.itemById(self.logger.PALETTE_ID))
        self.assertIsNone(self.ui.commandDefinitions.itemById(self.logger.PALETTE_ID_SHOW))
        self.assertEqual(self.logger.handlers, [])

    def test_stop_without_consent_is_clean(self):
        self.logger.stop(None)
        self.assertEqual(self.ui.messages, [])
        self.assertFalse(os.path.exists(self.logger.DATA_DIR))

    def test_restart_after_stop_starts_a_new_file(self):
        self.consent()
        self.logger.stop(None)
        self.logger.run(None)
        self.palette = self.ui.palettes.itemById(self.logger.PALETTE_ID)
        self.assertEqual(self.consent()['status'], 'recording')
        self.assertEqual(len(self.logs()), 2)

    def test_stale_toolbar_control_from_a_crash_is_replaced(self):
        self.logger.stop(None)
        definition = self.ui.commandDefinitions.addButtonDefinition(self.logger.PALETTE_ID_SHOW, '', '', '')
        panel = self.ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        stale = panel.controls.addCommand(definition)
        self.logger.run(None)
        self.assertTrue(stale.deleted)
        self.assertIsNotNone(panel.controls.itemById(self.logger.PALETTE_ID_SHOW))

    def test_stale_definition_from_a_crash_does_not_block_palette(self):
        self.logger.stop(None)
        self.ui.commandDefinitions.addButtonDefinition(self.logger.PALETTE_ID_SHOW, '', '', '')
        self.logger.run(None)
        self.assertIsNotNone(self.ui.palettes.itemById(self.logger.PALETTE_ID))

    def test_command_map_survives_a_tab_that_raises(self):
        class BrokenTab:
            id = 'BrokenTab'
            @property
            def toolbarPanels(self):
                raise RuntimeError('seen on a real toolbar tab')
        self.ui.allToolbarTabs.items.insert(0, BrokenTab())
        self.consent()
        commands = [r for r in self.records() if r['record'] == 'command_map'][0]['commands']
        self.assertEqual(commands['ExtrudeCommand'], [['SolidTab', 'SolidCreatePanel']])
        self.assertEqual([r for r in self.records() if r['record'] == 'error'], [])

    def test_command_map_records_toolbar_locus(self):
        self.consent()
        commands = [r for r in self.records() if r['record'] == 'command_map'][0]['commands']
        self.assertEqual(commands['ExtrudeCommand'], [['SolidTab', 'SolidCreatePanel']])

    def test_closing_palette_warns_only_while_recording(self):
        self.palette.closed.fire(fake_adsk.Args())
        self.assertEqual(self.ui.messages, [])
        self.consent()
        self.palette.closed.fire(fake_adsk.Args())
        self.assertIn('still recording', self.ui.messages[0])

    def test_recording_panel_cannot_be_dismissed(self):
        self.assertFalse(self.palette.has_close_button)
        self.consent()
        self.palette.isVisible = False
        self.palette.closed.fire(fake_adsk.Args())
        self.assertTrue(self.palette.isVisible)

    def test_status_reports_the_true_state_for_a_reloaded_panel(self):
        self.assertEqual(self.send('status'), {'status': 'idle'})
        self.consent()
        self.assertEqual(self.send('status'), {'status': 'recording', 'participant_code': 'P03', 'review_open': False,
                                               'participant_check': False, 'unanswered': [], 'health': 'ok'})
        self.send('review', open=True)
        self.assertTrue(self.send('status')['review_open'])
        self.send('finish')
        self.assertEqual(self.send('status'), {'status': 'idle'})

    def test_review_keeps_recording_until_finish(self):
        self.consent()
        self.send('review', open=True)
        self.send('review', open=True)
        self.app.documentSaved.fire(fake_adsk.Args(document=self.app.activeDocument))
        self.send('review', open=False)
        self.assertEqual([r['state'] for r in self.records() if r['record'] == 'review'], ['opened', 'closed'])
        self.assertEqual(len(self.events('document_saved')), 1)
        self.assertEqual(len(self.ui.commandStarting.listeners), 1)

    def test_finish_ends_session_and_keeps_only_known_review_items(self):
        self.consent()
        reply = self.send('finish', checked=['models_saved', 'SECRET-USERNAME'])
        self.assertEqual(reply, {'status': 'finished', 'participant_code': 'P03', 'questionnaire_url': None})
        last = self.records()[-1]
        self.assertEqual((last['record'], last['end_source'], last['review_checked']), ('session_end', 'palette', ['models_saved']))
        self.assertEqual(self.ui.commandStarting.listeners, [])
        self.assertIsNotNone(self.ui.palettes.itemById(self.logger.PALETTE_ID))

    def test_finish_is_never_gated_on_the_checklist(self):
        self.consent()
        self.assertEqual(self.send('finish')['status'], 'finished')
        self.assertEqual(self.records()[-1]['review_checked'], [])

    def test_addin_stop_is_marked_and_still_shows_the_path(self):
        self.consent()
        path = self.logs()[0]
        self.logger.stop(None)
        last = self.records()[-1]
        self.assertEqual((last['end_source'], last['review_checked']), ('addin_stop', None))
        self.assertIn(path, self.ui.messages[-1])

    def test_researcher_still_gets_the_path_after_a_palette_finish(self):
        self.consent()
        path = self.logs()[0]
        self.send('finish')
        self.logger.stop(None)
        self.assertIn(path, self.ui.messages[-1])

    def test_questionnaire_link_carries_the_code_and_only_after_finish(self):
        self.logger.QUESTIONNAIRE_URL = 'https://survey.example/s?code={code}'
        opened = []
        self.logger.webbrowser = fake_adsk.Args(open=lambda url: opened.append(url) or True)
        self.consent()
        self.assertEqual(self.send('finish')['questionnaire_url'], 'https://survey.example/s?code=P03')
        self.assertEqual(self.send('open_questionnaire'), {'status': 'opened'})
        self.assertEqual(opened, ['https://survey.example/s?code=P03'])

    def test_missing_questionnaire_link_reports_an_error(self):
        self.consent()
        self.send('finish')
        self.assertEqual(self.send('open_questionnaire')['status'], 'error')

    def capture_target(self, name='Bracket-e1 v2'):
        self.logger.CAPTURE_TARGETS = True
        self.app.activeDocument = fake_adsk.Args(name=name)
        reply = self.send('capture_target')
        self.logger.CAPTURE_TARGETS = False
        self.app.activeDocument = fake_adsk.Args(name='Bracket v5')
        return reply

    def checks(self):
        return [r for r in self.records() if r['record'] == 'target_check']

    def test_check_matches_a_captured_target_at_high_accuracy(self):
        self.logger.PARTICIPANT_CHECK = True
        self.assertEqual(self.capture_target()['model'], 'Bracket')
        self.consent()
        self.assertEqual(self.send('check_target'), {'status': 'checked', 'result': 'match'})
        self.assertEqual(self.app.activeProduct.rootComponent.accuracy_asked, 3)
        check = self.checks()[0]
        self.assertEqual((check['result'], check['failed_measures'], check['in_review']), ('match', [], False))
        self.assertEqual(check['model_id'], self.logger.modelId(fake_adsk.Args(name='Bracket v1')))

    def test_each_press_counts_and_a_changed_model_stops_matching(self):
        self.logger.PARTICIPANT_CHECK = True
        self.capture_target()
        self.consent()
        self.send('check_target')
        self.app.activeProduct.rootComponent.props = fake_adsk.PhysicalProperties(volume=10.5)
        self.assertEqual(self.send('check_target')['result'], 'no_match')
        self.send('review', open=True)
        self.send('check_target')
        self.assertEqual([c['in_review'] for c in self.checks()], [False, False, True])
        self.assertEqual(self.checks()[1]['failed_measures'], ['volume'])
        self.logger.stop(None)
        self.assertEqual(self.records()[-1]['event_counts']['target_check'], 3)

    def test_each_measure_can_fail_alone(self):
        for changed, measure in [(dict(area=21.0), 'area'), (dict(center=(1.5, 2.0, 3.0)), 'center'),
                                 (dict(moments=(8.0, 5.0, 6.0, 0.0, 0.0, 0.0)), 'moments')]:
            target = self.logger.targetFingerprint()
            self.app.activeProduct.rootComponent.props = fake_adsk.PhysicalProperties(**changed)
            self.assertEqual(self.logger.failedMeasures(self.logger.targetFingerprint(), target, 0.0005), [measure])
            self.app.activeProduct.rootComponent.props = fake_adsk.PhysicalProperties()

    def test_change_of_material_is_not_a_change_of_shape(self):
        self.logger.PARTICIPANT_CHECK = True
        self.capture_target()
        self.consent()
        self.app.activeProduct.rootComponent.props = fake_adsk.PhysicalProperties(moments=(8.0, 10.0, 12.0, 0.0, 0.0, 0.0), density=4.0)
        self.assertEqual(self.send('check_target')['result'], 'match')

    def test_values_are_never_logged_or_shown(self):
        self.logger.PARTICIPANT_CHECK = True
        self.capture_target()
        self.consent()
        self.app.activeProduct.rootComponent.props = fake_adsk.PhysicalProperties(volume=123.456)
        reply = self.send('check_target')
        self.assertEqual(sorted(reply), ['result', 'status'])
        with open(self.logs()[0], encoding='utf-8') as f:
            self.assertNotIn('123.456', f.read())

    def test_model_without_a_target_says_so_and_still_counts(self):
        self.logger.PARTICIPANT_CHECK = True
        self.consent()
        self.assertEqual(self.send('check_target')['result'], 'no_target')
        self.assertEqual(len(self.checks()), 1)
        self.assertIsNone(self.records()[0]['targets_sha256'])

    def test_unreadable_geometry_is_reported_and_counted(self):
        self.logger.PARTICIPANT_CHECK = True
        self.capture_target()
        self.consent()
        self.app.activeProduct = fake_adsk.Args(productType='DrawingProductType')
        self.assertEqual(self.send('check_target')['result'], 'unreadable')
        self.assertEqual(self.checks()[0]['result'], 'unreadable')

    def test_targets_file_is_identified_and_holds_no_names(self):
        self.capture_target()
        self.consent()
        self.assertEqual(len(self.records()[0]['targets_sha256']), 64)
        with open(self.logger.TARGETS_PATH, encoding='utf-8') as f:
            self.assertNotIn('Bracket', f.read())

    def test_capture_is_off_by_default_and_blocks_recording_when_on(self):
        self.assertEqual(self.send('capture_target')['status'], 'error')
        self.assertFalse(os.path.exists(self.logger.TARGETS_PATH))
        self.logger.CAPTURE_TARGETS = True
        self.addCleanup(setattr, self.logger, 'CAPTURE_TARGETS', False)
        self.assertEqual(self.consent()['status'], 'error')
        self.assertFalse(os.path.exists(self.logger.DATA_DIR))

    def test_panel_names_the_suffix_capture_needs(self):
        with open(self.logger.PALETTE_HTML, encoding='utf-8') as f:
            self.assertIn('<i>model</i>' + self.logger.TARGET_SUFFIX, f.read())

    def test_unreadable_targets_file_is_never_overwritten(self):
        with open(self.logger.TARGETS_PATH, 'w') as f:
            f.write('{"models": {"half-written')
        reply = self.capture_target()
        self.assertEqual(reply['status'], 'error')
        with open(self.logger.TARGETS_PATH) as f:
            self.assertEqual(f.read(), '{"models": {"half-written')

    def test_ambient_commands_are_recorded_and_skip_the_timeline_scan(self):
        self.consent()
        self.assertEqual(self.records()[0]['ambient_commands'], sorted(self.logger.AMBIENT_COMMANDS))
        self.ui.commandTerminated.fire(fake_adsk.Args(commandId='CommitCommand', terminationReason=1))
        self.assertIsNone(self.events('command_terminated')[0]['health_errors'])

    def test_capture_refuses_a_document_that_is_not_a_target(self):
        self.assertEqual(self.capture_target(name='Bracket v2')['status'], 'error')
        self.assertFalse(os.path.exists(self.logger.TARGETS_PATH))

    def test_check_refused_once_recording_stops(self):
        self.consent()
        self.send('finish')
        self.assertEqual(self.send('check_target')['status'], 'error')

    def test_panel_offers_every_review_item(self):
        with open(self.palette.htmlFileURL, encoding='utf-8') as f:
            html = f.read()
        for item in self.logger.REVIEW_ITEMS:
            self.assertIn(f'value="{item}"', html)




class HealthCase(LoggerBase):
    def check(self):
        self.app.fireCustomEvent(self.logger.HEALTH_EVENT_ID)
        self.app.runQueued()

    def heartbeats(self):
        return [r for r in self.records() if r['record'] == 'heartbeat']

    def health_pushes(self):
        return [json.loads(data) for action, data in self.palette.sent if action == 'health']

    def test_healthy_session_writes_heartbeats_that_are_not_events(self):
        self.consent()
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='ExtrudeCommand', isCanceled=False))
        self.check()
        self.check()
        beats = self.heartbeats()
        self.assertEqual([b['checks_failed'] for b in beats], [[], []])
        self.assertEqual([b['events_since'] for b in beats], [2, 0])
        self.assertEqual(self.records()[0]['health_interval_s'], self.logger.HEALTH_INTERVAL_S)
        self.assertEqual(self.ui.messages, [])
        self.assertEqual(self.health_pushes(), [])

    def test_timer_queues_checks_on_its_own(self):
        self.logger.HEALTH_INTERVAL_S = 0.01
        self.consent()
        deadline = time.monotonic() + 2
        while not self.app.queued and time.monotonic() < deadline:
            time.sleep(0.01)
        self.app.runQueued()
        self.assertTrue(self.heartbeats())

    def test_stop_ends_the_timer_and_the_custom_event(self):
        self.logger.HEALTH_INTERVAL_S = 0.01
        self.consent()
        self.send('finish')
        self.assertEqual(self.app.customEvents, {})
        self.assertFalse([t for t in threading.enumerate() if t.name == self.logger.HEALTH_EVENT_ID])

    def test_stale_custom_event_from_a_crash_is_replaced(self):
        self.app.registerCustomEvent(self.logger.HEALTH_EVENT_ID)
        self.consent()
        self.check()
        self.assertEqual(len(self.heartbeats()), 1)

    def test_checks_that_cannot_start_raise_the_problem(self):
        self.app.registerCustomEvent = lambda event_id: None
        self.assertEqual(self.consent()['status'], 'recording')
        self.assertIn('recording checks could not run', self.ui.messages[0])
        self.assertEqual(self.send('status')['health'], 'problem')

    def failing(self, name):
        failed = self.logger.runHealthChecks()
        self.assertIn(name, failed)
        self.assertEqual(self.send('status')['health'], 'problem')
        self.assertEqual(self.health_pushes()[-1]['health'], 'problem')
        self.assertEqual(len(self.ui.messages), 1)
        return failed

    @unittest.skipIf(os.name == 'nt', 'Windows refuses to move or delete an open file. test_platform checks that.')
    def test_moved_log_file_is_raised(self):
        self.consent()
        os.rename(self.logs()[0], self.logs()[0] + '.moved')
        self.failing('file_in_place')

    @unittest.skipIf(os.name == 'nt', 'Windows refuses to move or delete an open file. test_platform checks that.')
    def test_deleted_log_file_is_raised(self):
        self.consent()
        os.remove(self.logs()[0])
        self.failing('file_in_place')

    def test_closed_log_file_is_raised(self):
        self.consent()
        self.logger._log_file.close()
        self.assertEqual(self.failing('file_open'), ['file_open', 'write_ok'])
        self.logger.stopHealthChecks()
        self.logger._log_file = None

    def test_failed_write_is_raised(self):
        self.consent()
        real = self.logger.syncLog
        def broken():
            raise OSError(28, 'No space left on device')
        self.logger.syncLog = broken
        self.failing('write_ok')
        self.logger.syncLog = real

    def test_low_disk_is_raised(self):
        self.consent()
        self.logger.MIN_FREE_BYTES = 1 << 62
        self.failing('disk_space')

    def test_lost_binding_is_raised(self):
        self.consent()
        self.logger._bound.pop()
        self.failing('handlers_bound')

    def test_burst_of_capture_errors_is_raised(self):
        self.consent()
        for _ in range(self.logger.MAX_ERRORS_PER_CHECK + 1):
            self.logger.recordError('test')
        self.failing('errors_steady')

    def test_panel_deleted_by_fusion_is_rebuilt_at_the_next_check(self):
        self.consent()
        self.palette.deleted = True
        self.assertEqual(self.logger.runHealthChecks(), [])
        rebuilt = self.ui.palettes.itemById(self.logger.PALETTE_ID)
        self.assertIsNotNone(rebuilt)
        self.assertTrue(self.heartbeats()[-1]['panel_restored'])
        self.palette = rebuilt
        self.assertEqual(self.send('status')['status'], 'recording')
        self.assertEqual(self.ui.messages, [])

    def test_panel_rebuilt_as_soon_as_a_document_opens(self):
        self.consent()
        self.palette.deleted = True
        self.app.documentActivating.fire(fake_adsk.Args(document=self.app.activeDocument))
        self.assertIsNotNone(self.ui.palettes.itemById(self.logger.PALETTE_ID))

    def test_panel_that_cannot_be_rebuilt_is_raised(self):
        self.consent()
        self.palette.deleted = True
        self.ui.palettes.add = lambda *a, **k: None
        self.assertIn('panel_missing', self.logger.runHealthChecks())
        self.assertEqual(len(self.ui.messages), 1)

    def test_no_open_design_is_not_a_missing_panel(self):
        self.consent()
        self.palette.deleted = True
        self.ui.palettes.add = lambda *a, **k: None
        self.app.activeDocument = None
        self.assertEqual(self.logger.runHealthChecks(), [])
        self.assertEqual(self.ui.messages, [])

    def test_panel_rebuild_that_raises_is_raised_not_fatal(self):
        self.consent()
        self.palette.deleted = True
        def broken(*a, **k):
            raise RuntimeError('seen when no design is open')
        self.ui.palettes.add = broken
        self.assertIn('panel_missing', self.logger.runHealthChecks())

    def test_hidden_panel_comes_back_without_an_alarm(self):
        self.consent()
        self.palette.isVisible = False
        self.assertEqual(self.logger.runHealthChecks(), [])
        self.assertTrue(self.palette.isVisible)
        self.assertTrue(self.heartbeats()[-1]['panel_restored'])
        self.assertEqual(self.ui.messages, [])

    def test_write_error_at_start_leaves_nothing_half_running(self):
        real = self.logger.syncLog
        def broken():
            raise OSError(28, 'No space left on device')
        self.logger.syncLog = broken
        self.assertEqual(self.consent()['status'], 'error')
        self.logger.syncLog = real
        self.assertEqual(self.send('status')['status'], 'idle')
        self.assertEqual(self.ui.commandStarting.listeners, [])
        self.assertIsNone(self.logger._health_thread)
        self.assertEqual(self.consent()['status'], 'recording')

    def test_write_error_at_stop_still_ends_the_session(self):
        self.consent()
        real = self.logger.syncLog
        def broken():
            raise OSError(28, 'No space left on device')
        self.logger.syncLog = broken
        self.assertEqual(self.send('finish')['status'], 'error')
        self.logger.syncLog = real
        self.assertEqual(self.send('status')['status'], 'idle')
        self.assertEqual(self.ui.commandStarting.listeners, [])
        self.assertEqual(self.app.customEvents, {})

    def test_event_that_never_bound_is_raised(self):
        def refuse(handler):
            raise RuntimeError('seen after an API change')
        self.app.documentSaved.add = refuse
        self.consent()
        self.assertIn('handlers_bound', self.logger.runHealthChecks())

    def test_a_new_problem_interrupts_again_while_another_is_showing(self):
        self.consent()
        self.logger.MIN_FREE_BYTES = 1 << 62
        self.logger.runHealthChecks()
        self.logger.runHealthChecks()
        self.assertEqual(len(self.ui.messages), 1)
        self.logger._bound.pop()
        self.logger.runHealthChecks()
        self.assertEqual(len(self.ui.messages), 2)

    def test_one_message_per_problem_and_recovery_is_shown_and_logged(self):
        self.consent()
        self.logger.MIN_FREE_BYTES = 1 << 62
        self.check()
        self.check()
        self.assertEqual(len(self.ui.messages), 1)
        self.logger.MIN_FREE_BYTES = 0
        self.check()
        self.assertEqual(self.send('status')['health'], 'ok')
        self.assertEqual([p['health'] for p in self.health_pushes()], ['problem', 'ok'])
        self.assertEqual([b['checks_failed'] for b in self.heartbeats()], [['disk_space'], ['disk_space'], []])
        self.logger.MIN_FREE_BYTES = 1 << 62
        self.check()
        self.assertEqual(len(self.ui.messages), 2)

    @unittest.skipIf(os.name == 'nt', 'Windows refuses to move or delete an open file. test_platform checks that.')
    def test_problem_message_names_no_path(self):
        self.consent()
        os.remove(self.logs()[0])
        self.logger.runHealthChecks()
        self.assertNotIn(self.tmp.name, self.ui.messages[0] + self.send('status')['health_message'])


class PersistenceCase(LoggerBase):
    def test_restart_keeps_the_first_file_unchanged_and_read_only(self):
        self.consent()
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='ExtrudeCommand', isCanceled=False))
        self.send('finish')
        first = self.logs()[0]
        with open(first, 'rb') as f:
            before = f.read()
        self.assertFalse(os.stat(first).st_mode & 0o222)
        self.consent()
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='ExtrudeCommand', isCanceled=False))
        self.logger.stop(None)
        self.assertEqual(len(self.logs()), 2)
        with open(first, 'rb') as f:
            self.assertEqual(f.read(), before)

    def test_sessions_in_the_same_second_get_their_own_files(self):
        for _ in range(3):
            self.consent()
            self.send('finish')
        self.assertEqual(len(set(self.logs())), 3)

    def test_an_existing_name_is_refused_not_truncated(self):
        self.logger.uuid = fake_adsk.Args(uuid4=lambda: fake_adsk.Args(hex='aaaaaaaaaaaaaaaa'))
        with mock.patch('time.strftime', lambda fmt, t: '20261101T000000Z'):
            self.consent()
            self.send('finish')
            first = self.logs()[0]
            size = os.path.getsize(first)
            self.assertEqual(self.consent()['status'], 'error')
        self.assertEqual(os.path.getsize(first), size)


class ButtonCase(LoggerBase):
    def button(self):
        return self.ui.commandDefinitions.itemById(self.logger.PALETTE_ID_SHOW)

    def control(self):
        return self.ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel').controls.itemById(self.logger.PALETTE_ID_SHOW)

    def press(self):
        self.button().execute()
        self.palette = self.ui.palettes.itemById(self.logger.PALETTE_ID)

    def test_button_is_on_the_toolbar_and_says_start_when_idle(self):
        self.assertTrue(self.control().isPromoted)
        self.assertEqual(self.button().name, 'Start recording')
        self.assertTrue(self.button().resourceFolder.endswith(os.path.join('resources', 'start')))
        self.assertTrue(os.path.isdir(self.button().resourceFolder))

    def test_button_follows_the_recording_state(self):
        self.consent()
        self.assertEqual(self.button().name, 'Stop recording')
        self.assertTrue(os.path.isdir(self.button().resourceFolder))
        self.send('finish')
        self.assertEqual(self.button().name, 'Start recording')

    def test_pressing_start_never_records_without_consent(self):
        self.press()
        self.assertFalse(os.path.exists(self.logger.DATA_DIR))
        self.assertEqual(self.ui.commandStarting.listeners, [])

    def test_pressing_stop_opens_the_checklist_and_keeps_recording(self):
        self.consent()
        self.press()
        self.assertEqual([r['state'] for r in self.records() if r['record'] == 'review'], ['opened'])
        self.assertEqual(json.loads(self.palette.sent[-1][1])['review_open'], True)
        self.assertEqual(len(self.ui.commandStarting.listeners), 1)

    def test_pressing_the_button_rebuilds_a_deleted_panel(self):
        self.consent()
        self.palette.deleted = True
        self.press()
        self.assertIsNotNone(self.palette)
        self.assertEqual(self.send('status')['status'], 'recording')

    def test_relaunch_after_fusion_closed_says_how_the_last_recording_ended(self):
        self.consent(code='P07')
        self.logger.stop(None)
        self.logger.run(None)
        self.palette = self.ui.palettes.itemById(self.logger.PALETTE_ID)
        previous = self.send('status')['previous']
        self.assertEqual(previous['ended'], 'addin_stop')
        self.assertNotIn('P07', json.dumps(previous))

    def test_relaunch_after_a_crash_reports_a_cut_short_session(self):
        self.consent(code='P07')
        self.logger._log_file.close()
        self.logger._log_file = None
        self.logger.stopHealthChecks()
        self.assertEqual(self.send('status')['previous']['ended'], 'cut_short')

    def test_half_written_last_line_still_reports_a_cut_short_session(self):
        self.consent(code='P07')
        self.ui.activeSelectionChanged.fire(fake_adsk.Args(currentSelection=[fake_adsk.Selection()] * 50))
        self.logger._log_file.write('{"record":"event","event":"sel')
        self.logger._log_file.close()
        self.logger._log_file = None
        self.logger.stopHealthChecks()
        previous = self.send('status')['previous']
        self.assertEqual(previous['ended'], 'cut_short')
        self.assertNotIn('P07', json.dumps(previous))
        self.assertFalse(os.access(self.logs()[0], os.W_OK))

    def test_panel_never_fills_in_the_last_code(self):
        with open(self.logger.PALETTE_HTML, encoding='utf-8') as f:
            self.assertNotIn('previous.participant_code', f.read())

    @unittest.skipIf(os.name == 'nt', 'Windows has a read-only flag, not owner permissions.')
    def test_existing_loose_folder_is_tightened(self):
        os.makedirs(self.logger.DATA_DIR, mode=0o755)
        os.chmod(self.logger.DATA_DIR, 0o755)
        self.consent()
        self.assertEqual(os.stat(self.logger.DATA_DIR).st_mode & 0o777, 0o700)

    def test_clean_finish_leaves_no_notice(self):
        self.assertNotIn('previous', self.send('status'))
        self.consent()
        self.send('finish')
        self.assertNotIn('previous', self.send('status'))

    def test_add_in_loads_with_fusion(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'fusionLogger.manifest')) as f:
            self.assertTrue(json.load(f)['runOnStartup'])


class QuestionsCase(LoggerBase):
    def edit(self, name):
        self.app.activeDocument = fake_adsk.Args(name=name, isModified=True)
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='ExtrudeCommand', isCanceled=False))
        self.ui.commandTerminated.fire(fake_adsk.Args(commandId='ExtrudeCommand', terminationReason=1))

    def answers(self):
        return [r for r in self.records() if r['record'] == 'file_answers']

    def windows(self):
        return [(r['state'], r.get('outcome')) for r in self.records() if r['record'] == 'file_questions']

    def test_participant_check_is_off_and_hidden(self):
        self.consent()
        self.assertFalse(self.send('status')['participant_check'])
        self.assertEqual(self.send('check_target')['status'], 'error')
        self.assertEqual([r for r in self.records() if r['record'] == 'target_check'], [])

    def test_questions_never_come_before_an_edit(self):
        self.consent()
        self.app.activeDocument = fake_adsk.Args(name='027b v1')
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='SelectCommand', isCanceled=False))
        reply = self.send('file_done')
        self.assertEqual(reply['status'], 'error')
        self.assertIn('Make your changes', reply['message'])
        self.assertEqual(self.windows(), [])

    def test_looking_at_a_file_does_not_earn_questions(self):
        self.consent()
        self.app.activeDocument = fake_adsk.Args(name='027b v1', isModified=False)
        for command in ('OrbitCommand', 'MeasureCommand', 'SaveCommand'):
            self.ui.commandStarting.fire(fake_adsk.Args(commandId=command, isCanceled=False))
            self.ui.commandTerminated.fire(fake_adsk.Args(commandId=command, terminationReason=1))
        self.assertEqual(self.send('file_done')['status'], 'error')
        self.assertEqual(self.send('status')['unanswered'], [])

    def test_a_change_with_no_command_is_caught_by_the_health_check(self):
        self.consent()
        self.app.activeDocument = fake_adsk.Args(name='027b v1', isModified=True)
        self.logger.runHealthChecks()
        self.assertEqual(self.send('file_done')['questions'], {'file': '027b'})

    def test_answers_are_logged_against_the_file_and_timed_apart(self):
        self.consent()
        self.edit('027b v2')
        self.assertEqual(self.send('file_done')['questions'], {'file': '027b'})
        reply = self.send('file_answers', difficulty=6, note='  The second sketch drove both holes. ')
        self.assertNotIn('questions', reply)
        answer = self.answers()[0]
        self.assertEqual(answer['model_id'], self.logger.anonId('027b'))
        self.assertEqual((answer['difficulty'], answer['note'], answer['skipped']), (6, 'The second sketch drove both holes.', []))
        self.assertEqual(self.windows(), [('opened', None), ('closed', 'saved')])
        opened = [r for r in self.records() if r['record'] == 'file_questions'][0]
        self.assertLessEqual(opened['ts_ms'], answer['ts_ms'])
        self.assertEqual(reply['unanswered'], [])

    def test_switching_file_mid_form_keeps_the_answers_on_the_pressed_file(self):
        self.consent()
        self.edit('027b v1')
        self.send('file_done')
        self.app.activeDocument = fake_adsk.Args(name='012a v1')
        self.send('file_answers', difficulty=3, note='nothing')
        self.assertEqual(self.answers()[0]['model_id'], self.logger.anonId('027b'))

    def test_blank_or_invalid_answers_are_kept_as_skipped(self):
        self.consent()
        self.edit('027b v1')
        self.send('file_done')
        self.send('file_answers', difficulty=9, note='   ')
        self.assertEqual(self.answers()[0]['skipped'], ['difficulty', 'note'])
        self.edit('012a v1')
        self.send('file_done')
        self.send('file_answers', difficulty=True, note=5)
        self.assertEqual(self.answers()[1]['skipped'], ['difficulty', 'note'])

    def test_every_edited_file_is_listed_until_answered(self):
        self.consent()
        self.edit('016a v1')
        self.edit('027b v1')
        self.assertEqual(self.send('status')['unanswered'], ['016a', '027b'])
        self.app.activeDocument = fake_adsk.Args(name='016a v1')
        self.send('file_done')
        self.send('file_answers', difficulty=5, note='nothing')
        self.assertEqual(self.send('status')['unanswered'], ['027b'])

    def test_leaving_or_closing_a_file_updates_the_panel(self):
        self.consent()
        self.edit('027b v1')
        before = len(self.palette.sent)
        self.app.documentActivating.fire(fake_adsk.Args(document=fake_adsk.Args(name='012a v1')))
        self.app.documentClosing.fire(fake_adsk.Args(document=fake_adsk.Args(name='027b v1')))
        states = [json.loads(d) for a, d in self.palette.sent[before:] if a == 'state']
        self.assertEqual(len(states), 2)
        self.assertEqual(states[-1]['unanswered'], ['027b'])

    def test_cancel_and_stop_close_the_window_without_answers(self):
        self.consent()
        self.edit('027b v1')
        self.send('file_done')
        self.send('file_cancel')
        self.send('file_done')
        self.send('review', open=True)
        self.assertEqual(self.windows(), [('opened', None), ('closed', 'cancelled'), ('opened', None), ('closed', 'stopped')])
        self.assertEqual(self.answers(), [])
        self.assertEqual(self.send('status')['unanswered'], ['027b'])

    def test_answers_after_the_window_closed_are_refused(self):
        self.consent()
        self.edit('027b v1')
        self.assertEqual(self.send('file_answers', difficulty=4, note='x')['status'], 'error')
        self.assertEqual(self.answers(), [])

    def test_finish_counts_unanswered_files_and_is_never_gated(self):
        self.consent()
        self.edit('027b v1')
        self.send('file_done')
        self.assertEqual(self.send('finish')['status'], 'finished')
        end = self.records()[-1]
        self.assertEqual(end['unanswered_files'], 1)
        self.assertEqual(self.windows()[-1], ('closed', 'abandoned'))

    def test_file_names_reach_the_panel_but_never_the_log(self):
        self.consent()
        self.edit('SECRET-DOCUMENT-NAME v4')
        self.assertEqual(self.send('file_done')['questions'], {'file': 'SECRET-DOCUMENT-NAME'})
        self.send('file_answers', difficulty=2, note='nothing')
        with open(self.logs()[0], encoding='utf-8') as f:
            self.assertNotIn('SECRET-DOCUMENT', f.read())

    def test_panel_shows_the_fixed_wording(self):
        with open(self.palette.htmlFileURL, encoding='utf-8') as f:
            html = f.read()
        for text in ['Overall, how hard was it to make the changes to this model?',
                     '<span>Very easy</span><span>Very hard</span>',
                     'In one or two sentences: was there anything about how this model was built that affected your changes? '
                     "If so, where (for example a sketch, feature or plane)? If nothing stood out, write \'nothing\'.",
                     'After each file, the panel asks you two short questions about it.']:
            self.assertIn(text, html)
        self.assertEqual(html.count('name="difficulty"'), 7)
        self.assertNotIn('Check this model button', html)


if __name__ == '__main__':
    unittest.main()
