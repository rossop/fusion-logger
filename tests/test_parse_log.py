import contextlib, csv, importlib, io, json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'analysis'))
import fake_adsk
import parse_log

HEADER = {'record': 'session_start', 'session_id': 's1', 'seq': 0, 'ts_ms': 0, 'mono_ms': 0, 'schema_version': 1,
          'participant_code': 'P03', 'consent': True, 'consent_text_version': 'rec-1'}
END = {'record': 'session_end', 'error_count': 0}


def event(seq, ts, name='command_starting', model='m1', part='a', **fields):
    return {'record': 'event', 'session_id': 's1', 'seq': seq, 'ts_ms': ts, 'mono_ms': ts, 'event': name,
            'model_id': model, 'part_id': part, 'environment': 'part', **fields}


class ParserCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, records):
        path = os.path.join(self.tmp.name, 'log.jsonl')
        with open(path, 'w') as f:
            f.write(''.join(json.dumps(r) + '\n' for r in records))
        return path

    def run_parser(self, path, *extra):
        out = os.path.join(self.tmp.name, 'out')
        with contextlib.redirect_stderr(io.StringIO()) as stderr, contextlib.redirect_stdout(io.StringIO()):
            code = parse_log.main([path, '--out', out, *extra])
        self.stderr = stderr.getvalue()
        if code:
            return code, None, None
        with open(os.path.join(out, 'metrics.csv')) as f:
            metrics = list(csv.DictReader(f))
        with open(os.path.join(out, 'commands.csv')) as f:
            commands = list(csv.DictReader(f))
        return code, metrics, commands

    def session(self, *events):
        last = len(events) + 1
        return [HEADER, *events, {**END, 'seq': last, 'session_id': 's1', 'ts_ms': events[-1]['ts_ms'], 'mono_ms': events[-1]['ts_ms']}]

    def test_switching_matches_ch05_definition(self):
        path = self.write(self.session(
            event(1, 0, part='a'), event(2, 30000, part='b'), event(3, 60000, part='b'), event(4, 60000 + 30000, part='a')))
        _, metrics, _ = self.run_parser(path)
        session = metrics[0]
        self.assertEqual(session['switches'], '2')
        self.assertEqual(session['active_time_ms'], '90000')
        self.assertEqual(float(session['switches_per_active_min']), round(2 / 1.5, 4))

    def test_pause_is_excluded_from_active_time_and_part_time(self):
        path = self.write(self.session(event(1, 0), event(2, 1000), event(3, 1000 + parse_log.IDLE_GAP_MS + 1)))
        _, metrics, _ = self.run_parser(path)
        self.assertEqual(metrics[0]['active_time_ms'], '1000')
        self.assertEqual(json.loads(metrics[0]['part_time_ms']), {'a': 1000})
        self.assertEqual(int(metrics[0]['completion_time_ms']), 1000 + parse_log.IDLE_GAP_MS + 1)

    def test_events_without_a_part_do_not_count_as_switches(self):
        path = self.write(self.session(event(1, 0, part='a'), event(2, 10, part=None), event(3, 20, part='a')))
        _, metrics, _ = self.run_parser(path)
        self.assertEqual(metrics[0]['switches'], '0')

    def test_per_model_rows_and_map(self):
        name_hash = parse_log.model_hash('Bracket v2')
        path = self.write(self.session(event(1, 0, model=name_hash), event(2, 100, model='other')))
        map_path = os.path.join(self.tmp.name, 'map.json')
        with open(map_path, 'w') as f:
            json.dump({'Bracket': 'M00'}, f)
        _, metrics, commands = self.run_parser(path, '--model-map', map_path)
        self.assertEqual([m['scope'] for m in metrics], ['session', 'model', 'model'])
        self.assertEqual(metrics[1]['model'], 'M00')
        self.assertEqual(commands[0]['model'], 'M00')

    def test_time_on_another_model_is_not_credited_to_the_first(self):
        path = self.write(self.session(
            event(1, 0, model='m1'), event(2, 1000, model='m1'),
            event(3, 2000, model='m2'), event(4, 9000, model='m2'),
            event(5, 10000, model='m1'), event(6, 10500, model='m1')))
        _, metrics, _ = self.run_parser(path)
        first = [m for m in metrics if m['model'] == 'm1'][0]
        self.assertEqual(first['completion_time_ms'], '1500')
        self.assertEqual(first['active_time_ms'], '1500')

    def test_consent_screen_time_is_not_task_time(self):
        path = self.write(self.session(event(1, 0, name='logging_started'), event(2, 50000), event(3, 51000)))
        _, metrics, _ = self.run_parser(path)
        self.assertEqual(metrics[0]['completion_time_ms'], '1000')

    def test_end_review_is_not_task_time(self):
        opened = {'record': 'review', 'state': 'opened', 'session_id': 's1', 'seq': 2, 'ts_ms': 2000, 'mono_ms': 2000}
        path = self.write(self.session(event(1, 1000), opened, event(3, 9000, 'document_activating', model='m2'),
                                       event(4, 9500, 'document_saved', model='m2')))
        _, metrics, _ = self.run_parser(path)
        self.assertEqual([m['model'] for m in metrics if m['scope'] == 'model'], ['m1'])
        self.assertNotIn('end review', self.stderr)

    def test_returning_from_review_counts_again_and_review_commands_warn(self):
        opened = {'record': 'review', 'state': 'opened', 'session_id': 's1', 'seq': 2, 'ts_ms': 2000, 'mono_ms': 2000}
        closed = {'record': 'review', 'state': 'closed', 'session_id': 's1', 'seq': 4, 'ts_ms': 4000, 'mono_ms': 4000}
        path = self.write(self.session(event(1, 1000), opened, event(3, 3000, command_id='ExtrudeCommand'), closed,
                                       event(5, 5000, command_id='FilletCommand')))
        _, metrics, commands = self.run_parser(path)
        self.assertEqual(metrics[0]['commands'], '2')
        self.assertEqual(len(commands), 2)
        self.assertIn('1 commands ran during the end review', self.stderr)

    def test_target_checks_are_counted_without_changing_time(self):
        def check(seq, ts, result):
            return {'record': 'target_check', 'session_id': 's1', 'seq': seq, 'ts_ms': ts, 'mono_ms': ts, 'model_id': 'm1', 'result': result}
        path = self.write(self.session(event(1, 1000), event(2, 3000), check(3, 90000, 'no_match'), check(4, 95000, 'match'), check(5, 96000, 'no_target')))
        _, metrics, _ = self.run_parser(path)
        model = [m for m in metrics if m['scope'] == 'model'][0]
        self.assertEqual(model['completion_time_ms'], '2000')
        self.assertEqual(model['target_checks'], '3')
        self.assertEqual((model['target_first_matched'], model['target_last_matched']), ('False', 'True'))
        self.assertEqual(metrics[0]['target_checks'], '3')
        self.assertEqual(metrics[0]['target_first_matched'], '')

    def test_command_sequence_duration_and_errors(self):
        path = self.write(self.session(
            event(1, 0, command_id='Extrude'),
            event(2, 500, name='command_terminated', command_id='Extrude', termination_reason='cancelled', health_errors=0),
            event(3, 600, command_id='UndoCommand'),
            event(4, 700, name='command_terminated', command_id='UndoCommand', termination_reason='completed', health_errors=2)))
        _, metrics, commands = self.run_parser(path)
        self.assertEqual([c['command_id'] for c in commands], ['Extrude', 'UndoCommand'])
        self.assertEqual(commands[0]['duration_ms'], '500')
        self.assertEqual(metrics[0]['error_commands'], '1')
        self.assertEqual(metrics[0]['cancelled_commands'], '1')
        self.assertEqual(metrics[0]['new_health_errors'], '2')

    def test_ambient_set_comes_from_the_log_when_present(self):
        header = {**HEADER, 'ambient_commands': ['OrbitCommand']}
        path = self.write([header, event(1, 0, command_id='OrbitCommand'), event(2, 500, command_id='OrbitCommand'),
                           event(3, 1000, command_id='SelectCommand'),
                           {**END, 'seq': 4, 'session_id': 's1', 'ts_ms': 1000, 'mono_ms': 1000}])
        _, metrics, _ = self.run_parser(path)
        self.assertEqual(metrics[0]['commands'], '1')

    def test_growing_multi_select_counts_each_entity_once(self):
        faces = [{'type': 'adsk::fusion::BRepFace', 'token_hash': t, 'area': None, 'length': None, 'bbox': None} for t in 'abc']
        path = self.write(self.session(
            event(1, 0, name='selection_changed', selections=faces[:1]),
            event(2, 10, name='selection_changed', selections=faces[:2]),
            event(3, 20, name='selection_changed', selections=faces[:3]),
            event(4, 30, name='selection_changed', selections=faces[:2])))
        _, metrics, _ = self.run_parser(path)
        self.assertEqual((metrics[0]['selections'], metrics[0]['repeat_selections']), ('3', '0'))

    def test_one_bad_log_does_not_stop_the_others(self):
        good = self.write(self.session(event(1, 0), event(2, 1000)))
        bad = os.path.join(self.tmp.name, 'bad.jsonl')
        with open(bad, 'w') as f:
            f.write(json.dumps({**HEADER, 'consent': False}) + '\n')
        out = os.path.join(self.tmp.name, 'out')
        with contextlib.redirect_stderr(io.StringIO()) as stderr, contextlib.redirect_stdout(io.StringIO()):
            code = parse_log.main([good, bad, '--out', out])
        self.assertEqual(code, 2)
        self.assertIn('rejected', stderr.getvalue())
        with open(os.path.join(out, 'metrics.csv')) as f:
            self.assertEqual(len(list(csv.DictReader(f))), 2)

    def test_repeat_selection_counts_same_entity_twice(self):
        face = {'type': 'face', 'area': 1.0, 'length': None, 'bbox': [0, 0, 0, 1, 1, 1]}
        other = {'type': 'face', 'area': 2.0, 'length': None, 'bbox': [0, 0, 0, 2, 2, 2]}
        path = self.write(self.session(
            event(1, 0, name='selection_changed', selections=[face]),
            event(2, 10, name='selection_changed', selections=[other]),
            event(3, 20, name='selection_changed', selections=[face])))
        _, metrics, _ = self.run_parser(path)
        self.assertEqual(metrics[0]['selections'], '3')
        self.assertEqual(metrics[0]['repeat_selections'], '1')

    def test_features_without_geometry_are_told_apart_by_token(self):
        feature = {'type': 'adsk::fusion::ExtrudeFeature', 'area': None, 'length': None, 'bbox': None}
        path = self.write(self.session(
            event(1, 0, name='selection_changed', selections=[{**feature, 'token_hash': 'a'}]),
            event(2, 10, name='selection_changed', selections=[{**feature, 'token_hash': 'b'}]),
            event(3, 20, name='selection_changed', selections=[{**feature, 'token_hash': 'a'}])))
        _, metrics, _ = self.run_parser(path)
        self.assertEqual(metrics[0]['repeat_selections'], '1')

    def test_ambient_commands_are_kept_as_rows_but_not_counted(self):
        path = self.write(self.session(
            event(1, 0, command_id='SelectCommand'),
            event(2, 10, name='command_terminated', command_id='SelectCommand', termination_reason='cancelled'),
            event(3, 20, command_id='AutoSaveFilesCommand'),
            event(4, 30, command_id='Extrude')))
        _, metrics, commands = self.run_parser(path)
        self.assertEqual(metrics[0]['commands'], '1')
        self.assertEqual(metrics[0]['cancelled_commands'], '0')
        self.assertEqual(len(commands), 3)

    def test_tool_restarting_itself_pairs_oldest_run_first(self):
        # Order seen in a real log: the new SketchDimension starts before the old one is pre-empted.
        path = self.write(self.session(
            event(1, 0, command_id='SketchDimension'),
            event(2, 5000, command_id='SketchDimension'),
            event(3, 5001, name='command_terminated', command_id='SketchDimension', termination_reason='preempted'),
            event(4, 9000, name='command_terminated', command_id='SketchDimension', termination_reason='cancelled')))
        _, _, commands = self.run_parser(path)
        self.assertEqual([c['duration_ms'] for c in commands], ['5001', '4000'])
        self.assertEqual([c['termination_reason'] for c in commands], ['preempted', 'cancelled'])

    def test_document_event_does_not_count_as_a_part_switch(self):
        # Order seen in a real log: document_activating has the new model but the old part.
        path = self.write(self.session(
            event(1, 0, model='m1', part='a'),
            event(2, 10, name='document_activating', model='m2', part='a'),
            event(3, 20, model='m2', part='b'),
            event(4, 30, model='m2', part='b')))
        _, metrics, _ = self.run_parser(path)
        second = [m for m in metrics if m['model'] == 'm2'][0]
        self.assertEqual(second['switches'], '0')
        self.assertEqual(json.loads(second['part_time_ms']), {'b': 10})

    def test_missing_consent_is_rejected(self):
        self.assertEqual(self.run_parser(self.write([{**HEADER, 'consent': False}]))[0], 2)

    def test_unknown_schema_is_rejected(self):
        self.assertEqual(self.run_parser(self.write([{**HEADER, 'schema_version': 99}]))[0], 2)

    def test_truncated_session_warns_but_parses(self):
        path = self.write([HEADER, event(1, 0)])
        code, metrics, _ = self.run_parser(path)
        self.assertEqual(code, 0)
        self.assertEqual(len(metrics), 2)
        self.assertIn('no session_end', self.stderr)

    def test_half_written_last_line_is_tolerated(self):
        path = self.write([HEADER, event(1, 0)])
        with open(path, 'a') as f:
            f.write('{"record":"ev')
        self.assertEqual(self.run_parser(path)[0], 0)

    def test_corrupt_middle_line_is_rejected(self):
        path = self.write([HEADER])
        with open(path, 'a') as f:
            f.write('not json\n' + json.dumps(event(1, 0)) + '\n')
        self.assertEqual(self.run_parser(path)[0], 2)

    def test_sequence_gap_and_clock_jump_warn(self):
        path = self.write(self.session(event(1, 0), {**event(3, 5000), 'mono_ms': 100}))
        self.run_parser(path)
        self.assertIn('sequence gap', self.stderr)
        self.assertIn('clock moved', self.stderr)

    def beat(self, seq, ts, failed=()):
        return {'record': 'heartbeat', 'session_id': 's1', 'seq': seq, 'ts_ms': ts, 'mono_ms': ts,
                'checks_failed': list(failed), 'events_since': 0, 'panel_restored': False}

    def test_steady_heartbeats_raise_nothing(self):
        header = {**HEADER, 'health_interval_s': 30}
        path = self.write([header, event(1, 1000), self.beat(2, 30000), self.beat(3, 60000), event(4, 70000),
                           {**END, 'seq': 5, 'session_id': 's1', 'ts_ms': 80000, 'mono_ms': 80000}])
        self.assertEqual(self.run_parser(path)[0], 0)
        self.assertNotIn('health', self.stderr)

    def test_heartbeat_gap_and_failed_checks_warn_but_parse(self):
        header = {**HEADER, 'health_interval_s': 30}
        path = self.write([header, self.beat(1, 30000), event(2, 40000), self.beat(3, 200000, ['disk_space']),
                           self.beat(4, 230000, ['disk_space', 'write_ok']),
                           {**END, 'seq': 5, 'session_id': 's1', 'ts_ms': 240000, 'mono_ms': 240000}])
        code, metrics, _ = self.run_parser(path)
        self.assertEqual(code, 0)
        self.assertEqual(metrics[0]['events'], '1')
        self.assertIn('no health check for 170 s after seq 1', self.stderr)
        self.assertIn('health checks failed: disk_space 2 times, write_ok 1 times', self.stderr)

    def test_older_logs_without_heartbeats_raise_nothing(self):
        path = self.write(self.session(event(1, 0), event(2, 500000)))
        self.run_parser(path)
        self.assertNotIn('health', self.stderr)

    def test_file_question_window_is_not_task_time(self):
        opened = {'record': 'file_questions', 'session_id': 's1', 'seq': 2, 'ts_ms': 10000, 'mono_ms': 10000, 'state': 'opened', 'model_id': 'm1'}
        closed = {**opened, 'seq': 4, 'ts_ms': 90000, 'mono_ms': 90000, 'state': 'closed', 'outcome': 'saved'}
        path = self.write(self.session(event(1, 0), opened, event(3, 50000, command_id='ExtrudeCommand'), closed, event(5, 95000)))
        _, metrics, _ = self.run_parser(path)
        self.assertEqual(metrics[0]['events'], '2')
        self.assertIn('file questions', self.stderr)

    def window(self, seq, ts, kind, state, model='m1'):
        return {'record': kind, 'session_id': 's1', 'seq': seq, 'ts_ms': ts, 'mono_ms': ts, 'state': state, 'model_id': model}

    def test_question_time_is_not_counted_when_returning_to_the_same_model(self):
        path = self.write(self.session(
            event(1, 0), event(2, 10000), self.window(3, 10000, 'file_questions', 'opened'),
            self.window(4, 50000, 'file_questions', 'closed'), event(5, 55000), event(6, 60000)))
        _, metrics, _ = self.run_parser(path)
        session, model = metrics
        self.assertEqual((model['completion_time_ms'], model['active_time_ms']), ('15000', '15000'))
        self.assertEqual((session['completion_time_ms'], session['active_time_ms']), ('15000', '15000'))
        self.assertEqual(model['questions_time_ms'], '40000')

    def test_review_time_is_not_counted_either(self):
        path = self.write(self.session(
            event(1, 0), self.window(2, 5000, 'review', 'opened'), self.window(3, 45000, 'review', 'closed'), event(4, 50000)))
        _, metrics, _ = self.run_parser(path)
        self.assertEqual(metrics[1]['completion_time_ms'], '0')

    def test_answers_reach_metrics_and_notes_stay_apart(self):
        answer = {'record': 'file_answers', 'session_id': 's1', 'seq': 4, 'ts_ms': 20000, 'mono_ms': 20000,
                  'model_id': 'm1', 'difficulty': 6, 'note': 'The second sketch', 'skipped': []}
        path = self.write(self.session(
            event(1, 0), event(2, 10000), self.window(3, 10000, 'file_questions', 'opened'), answer,
            self.window(5, 30000, 'file_questions', 'closed'), event(6, 40000)))
        _, metrics, _ = self.run_parser(path)
        model = metrics[1]
        self.assertEqual((model['answers'], model['difficulty'], model['questions_time_ms']), ('1', '6', '20000'))
        self.assertNotIn('second sketch', json.dumps(metrics))
        with open(os.path.join(self.tmp.name, 'out', 'notes.csv')) as f:
            self.assertEqual([r['note'] for r in csv.DictReader(f)], ['The second sketch'])

    def test_moving_between_models_is_not_a_new_health_error(self):
        path = self.write(self.session(*[
            event(i, i * 1000, 'command_terminated', model='m1' if i % 2 else 'm2', health_errors=3 if i % 2 else 0)
            for i in range(1, 6)]))
        _, metrics, _ = self.run_parser(path)
        self.assertEqual({m['new_health_errors'] for m in metrics}, {'0'})

    def test_old_ease_answers_are_reported_and_never_read_as_difficulty(self):
        answer = {'record': 'file_answers', 'session_id': 's1', 'seq': 2, 'ts_ms': 1000, 'mono_ms': 1000,
                  'model_id': 'm1', 'ease': 7, 'note': 'fine', 'skipped': []}
        _, metrics, _ = self.run_parser(self.write(self.session(event(1, 0), answer, event(3, 2000))))
        self.assertEqual(metrics[1]['difficulty'], '')
        self.assertIn('1 answers use the older ease scale', self.stderr)

    def test_notes_keep_their_exact_text(self):
        answer = {'record': 'file_answers', 'session_id': 's1', 'seq': 2, 'ts_ms': 1000, 'mono_ms': 1000,
                  'model_id': 'm1', 'difficulty': 3, 'note': '- the sketch was hard', 'skipped': []}
        self.run_parser(self.write(self.session(event(1, 0), answer, event(3, 2000))))
        out = os.path.join(self.tmp.name, 'out')
        if os.name != 'nt':
            # Windows has a read-only flag, not owner permissions.
            self.assertEqual(os.stat(out).st_mode & 0o777, 0o700)
            for name in ('metrics.csv', 'commands.csv', 'notes.csv', 'notes.jsonl'):
                self.assertEqual(os.stat(os.path.join(out, name)).st_mode & 0o777, 0o600, name)
        with open(os.path.join(out, 'notes.jsonl'), encoding='utf-8') as f:
            self.assertEqual(json.loads(f.readline())['note'], '- the sketch was hard')
        with open(os.path.join(out, 'notes.csv'), encoding='utf-8') as f:
            self.assertEqual(next(csv.DictReader(f))['note'], "'- the sketch was hard")

    def test_numbers_are_not_escaped(self):
        self.assertEqual([parse_log.spreadsheet_safe(v) for v in ('-5', '+3.2', -5, None)], ['-5', '+3.2', -5, None])

    def test_control_codes_from_a_log_are_not_printed_raw(self):
        self.run_parser(self.write([{**HEADER, 'consent_text_version': 'x\x1b[2J'}, event(1, 0), event(2, 1000)]))
        self.assertNotIn('\x1b', self.stderr)
        self.assertIn('\\x1b', self.stderr)

    def test_values_that_would_run_in_a_spreadsheet_are_escaped(self):
        path = self.write(self.session(event(1, 0, command_id='=HYPERLINK("http://x")'), event(2, 1000)))
        _, _, commands = self.run_parser(path)
        self.assertEqual(commands[0]['command_id'], '\'=HYPERLINK("http://x")')

    def test_unapproved_consent_text_warns(self):
        path = self.write(self.session(event(1, 0)))
        with open(path) as f:
            lines = f.readlines()
        lines[0] = json.dumps({**HEADER, 'consent_text_version': 'draft-1'}) + '\n'
        with open(path, 'w') as f:
            f.writelines(lines)
        self.run_parser(path)
        self.assertIn('not an approved version', self.stderr)

    def test_reads_a_log_written_by_the_real_add_in_code(self):
        app = fake_adsk.install()
        sys.modules.pop('fusionLogger', None)
        logger = importlib.import_module('fusionLogger')
        logger.DATA_DIR = os.path.join(self.tmp.name, 'data')
        logger.run(None)
        ui = app.userInterface
        args = fake_adsk.Args(action='consent', data=json.dumps({'participant_code': 'P00', 'consent': True}), returnData='')
        ui.palettes.itemById(logger.PALETTE_ID).incomingFromHTML.fire(args)
        ui.commandStarting.fire(fake_adsk.Args(commandId='ExtrudeCommand', isCanceled=False))
        ui.activeSelectionChanged.fire(fake_adsk.Args(currentSelection=[fake_adsk.Selection()]))
        ui.commandTerminated.fire(fake_adsk.Args(commandId='ExtrudeCommand', terminationReason=1))
        logger.stop(None)
        path = os.path.join(logger.DATA_DIR, os.listdir(logger.DATA_DIR)[0])
        code, metrics, commands = self.run_parser(path)
        self.assertEqual(code, 0)
        self.assertEqual(commands[0]['command_id'], 'ExtrudeCommand')
        self.assertEqual(commands[0]['termination_reason'], 'completed')
        self.assertEqual(metrics[0]['selections'], '1')
        self.assertEqual(metrics[0]['participant_code'], 'P00')

    def test_model_hash_matches_add_in(self):
        fake_adsk.install()
        sys.modules.pop('fusionLogger', None)
        logger = importlib.import_module('fusionLogger')
        self.assertEqual(parse_log.model_hash('Bracket v4'), logger.modelId(fake_adsk.Args(name='Bracket v1')))


if __name__ == '__main__':
    unittest.main()
