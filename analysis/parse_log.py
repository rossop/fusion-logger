"""Turn fusionLogger session logs into per-session and per-model metrics.

    python3 analysis/parse_log.py LOG.jsonl [LOG.jsonl ...] --out DIR [--model-map MAP.json]

MAP.json maps real document names to study codes, for example {"Bracket baseline": "M00"}.
It stays with the researcher: the names never enter the logs or the outputs.
"""
import argparse, csv, hashlib, json, os, re, sys
from collections import Counter

SUPPORTED_SCHEMA = 1
ANON_PREFIX = 'fusionLogger:'
# Gaps longer than this count as a pause, not work, and are left out of active time.
IDLE_GAP_MS = 60000
CLOCK_JUMP_MS = 1000
HEALTH_GAP_INTERVALS = 2
# Substrings because Fusion's internal command ids are not documented.
ERROR_COMMAND_RE = re.compile(r'delete|undo|abort', re.IGNORECASE)
# Fusion runs these around every tool and on a timer, and the last is the logger's own button. Counting them would measure Fusion, not the user.
AMBIENT_COMMANDS = {'SelectCommand', 'CommitCommand', 'AutoSaveFilesCommand', 'PaletteFusionLoggerShow'}


def ambient_commands(header):
    # Newer logs record the set the add-in used, so a change there cannot silently change the metrics here.
    return set(header.get('ambient_commands') or AMBIENT_COMMANDS)

METRIC_FIELDS = ['session_id', 'participant_code', 'scope', 'model', 'events', 'commands',
                 'completion_time_ms', 'active_time_ms', 'switches', 'switches_per_active_min',
                 'error_commands', 'cancelled_commands', 'new_health_errors', 'selections',
                 'repeat_selections', 'environment_counts', 'part_time_ms', 'target_checks',
                 'target_first_matched', 'target_last_matched', 'answers', 'difficulty', 'questions_time_ms']
NOTE_FIELDS = ['session_id', 'participant_code', 'model', 'note']
COMMAND_FIELDS = ['session_id', 'participant_code', 'model', 'seq', 'ts_ms', 'part_id', 'environment',
                  'command_id', 'duration_ms', 'termination_reason']


class LogRejected(Exception):
    pass


def model_hash(name):
    return hashlib.sha256((ANON_PREFIX + re.sub(r' v\d+$', '', name)).encode('utf-8')).hexdigest()[:16]


def read_session(path):
    records = []
    with open(path, encoding='utf-8') as f:
        for number, line in enumerate(f, 1):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Only a crash mid-write can do this, so it is fatal anywhere but the last line.
                if f.readline():
                    raise LogRejected(f'{path}: line {number} is not valid JSON')
    if not records or records[0].get('record') != 'session_start':
        raise LogRejected(f'{path}: no session_start record')
    header = records[0]
    if header.get('schema_version') != SUPPORTED_SCHEMA:
        raise LogRejected(f'{path}: schema {header.get("schema_version")} is not supported')
    if header.get('consent') is not True:
        raise LogRejected(f'{path}: no recorded consent')
    return header, records


def validate(path, header, records):
    warnings = []
    if records[-1].get('record') != 'session_end':
        warnings.append('no session_end: the session was cut short and may be missing events')
    elif records[-1].get('error_count'):
        warnings.append(f'{records[-1]["error_count"]} capture errors during the session')
    for previous, current in zip(records, records[1:]):
        if current['seq'] != previous['seq'] + 1:
            warnings.append(f'sequence gap: {previous["seq"]} to {current["seq"]}')
        drift = (current['ts_ms'] - previous['ts_ms']) - (current['mono_ms'] - previous['mono_ms'])
        if abs(drift) > CLOCK_JUMP_MS:
            warnings.append(f'clock moved by {drift} ms at seq {current["seq"]}')
    interval = header.get('health_interval_s')
    if interval:
        # The add-in checks itself on a timer. A long silence means Fusion was busy for that long, or the add-in stopped.
        marks = [r for r in records if r.get('record') in ('session_start', 'heartbeat')] + [records[-1]]
        for previous, current in zip(marks, marks[1:]):
            gap = current['ts_ms'] - previous['ts_ms']
            if gap > HEALTH_GAP_INTERVALS * interval * 1000:
                warnings.append(f'no health check for {gap // 1000} s after seq {previous["seq"]}')
        failed = Counter(name for r in records if r.get('record') == 'heartbeat' for name in r.get('checks_failed') or [])
        if failed:
            warnings.append('health checks failed: ' + ', '.join(f'{name} {count} times' for name, count in sorted(failed.items())))
    old_scale = sum(1 for r in records if r.get('record') == 'file_answers' and 'ease' in r)
    if old_scale:
        # Older logs asked for ease, where 7 meant very easy. Read as difficulty it would be backwards, so it is left out.
        warnings.append(f'{old_scale} answers use the older ease scale (7 = very easy) and are left out of difficulty')
    if not str(header.get('consent_text_version', '')).startswith('rec-'):
        warnings.append(f'consent text {header.get("consent_text_version")} is not an approved version')
    return [f'{os.path.basename(path)}: {w}' for w in warnings]


def part_events(events):
    # A document event names the incoming model but still carries the outgoing model's part.
    return [e for e in events if not e['event'].startswith('document_')]


def part_time(events):
    # A gap is credited to the part in scope when it began. Pauses are credited to nothing.
    times = Counter()
    events = part_events(events)
    for previous, current in zip(events, events[1:]):
        gap = current['ts_ms'] - previous['ts_ms']
        if gap <= IDLE_GAP_MS and previous.get('part_id'):
            times[previous['part_id']] += gap
    return dict(times)


def active_time(events):
    gaps = (b['ts_ms'] - a['ts_ms'] for a, b in zip(events, events[1:]))
    return sum(g for g in gaps if g <= IDLE_GAP_MS)


def count_switches(events):
    # Events with no part in scope (a drawing, say) neither start nor end a switch.
    parts = [e['part_id'] for e in part_events(events) if e.get('part_id')]
    return sum(1 for a, b in zip(parts, parts[1:]) if a != b)


def selection_keys(event):
    for selection in event.get('selections') or []:
        # Equal hashes mean the same entity. Fusion can issue different tokens for one entity,
        # so geometry is the fallback and a repeat count here is a lower bound.
        geometry = [selection.get('area'), selection.get('length'), selection.get('bbox')]
        # Features and dimensions report no geometry, which would make them all look like one entity.
        yield json.dumps([selection.get('type'), *(geometry if any(geometry) else [selection.get('token_hash')])])


def split_visits(events):
    # Time on another model belongs to that model, so a return visit starts a new window.
    # So does coming back from the questions or the checklist: their time is not the model's.
    visits = {}
    previous = object()
    for event in events:
        model_id = event.get('model_id')
        if model_id != previous or event.get('after_break'):
            visits.setdefault(model_id, []).append([])
        visits[model_id][-1].append(event)
        previous = model_id
    return visits


def split_at_breaks(events):
    segments = []
    for event in events:
        if not segments or event.get('after_break'):
            segments.append([])
        segments[-1].append(event)
    return segments


def target_checks(records):
    # Kept apart from events: pressing the button neither starts nor extends a model's time.
    checks = {}
    for record in records:
        if record.get('record') == 'target_check':
            checks.setdefault(record.get('model_id'), []).append(record)
    return checks


def metrics_for(visits, header, scope, model, checks=()):
    decided = [c['result'] == 'match' for c in checks if c.get('result') in ('match', 'no_match')]
    events = [event for visit in visits for event in visit]
    active = sum(active_time(visit) for visit in visits)
    switches = sum(count_switches(visit) for visit in visits)
    part_times = Counter()
    for visit in visits:
        part_times.update(part_time(visit))
    seen, current, repeats, selections = set(), set(), 0, 0
    # Per model, because each model's timeline has its own count: moving between models is not a new error.
    new_errors, last_errors = 0, {}
    for event in events:
        if event['event'] == 'selection_changed':
            # Each event carries the whole current selection, so only entities added since the last one are picks.
            keys = set(selection_keys(event))
            for key in keys - current:
                selections += 1
                repeats += key in seen
                seen.add(key)
            current = keys
        errors, model_id = event.get('health_errors'), event.get('model_id')
        if errors is not None:
            if model_id in last_errors and errors > last_errors[model_id]:
                new_errors += errors - last_errors[model_id]
            last_errors[model_id] = errors
    ambient = ambient_commands(header)
    started = [e for e in events if e['event'] == 'command_starting' and e.get('command_id') not in ambient]
    return {
        'session_id': header['session_id'],
        'participant_code': header['participant_code'],
        'scope': scope,
        'model': model,
        'events': len(events),
        'commands': len(started),
        'completion_time_ms': sum(visit[-1]['ts_ms'] - visit[0]['ts_ms'] for visit in visits),
        'active_time_ms': active,
        'switches': switches,
        'switches_per_active_min': round(switches / (active / 60000), 4) if active else None,
        'error_commands': sum(1 for e in started if ERROR_COMMAND_RE.search(e.get('command_id') or '')),
        'cancelled_commands': sum(1 for e in events if e.get('termination_reason') in ('cancelled', 'aborted') and e.get('command_id') not in ambient),
        'new_health_errors': new_errors,
        'selections': selections,
        'repeat_selections': repeats,
        'environment_counts': json.dumps(Counter(e.get('environment') for e in events), sort_keys=True),
        'part_time_ms': json.dumps(part_times, sort_keys=True),
        'target_checks': len(checks),
        # The first result shows whether the participant judged it right unaided. The later ones have had feedback.
        'target_first_matched': decided[0] if decided and scope == 'model' else None,
        'target_last_matched': decided[-1] if decided and scope == 'model' else None,
    }


def command_rows(events, header, codes):
    open_commands = {}
    rows = []
    for event in events:
        command = event.get('command_id')
        if event['event'] == 'command_starting':
            row = {'session_id': header['session_id'], 'participant_code': header['participant_code'],
                   'model': codes.get(event.get('model_id'), event.get('model_id')), 'seq': event['seq'],
                   'ts_ms': event['ts_ms'], 'part_id': event.get('part_id'),
                   'environment': event.get('environment'), 'command_id': command,
                   'duration_ms': None, 'termination_reason': None}
            rows.append(row)
            open_commands.setdefault(command, []).append(row)
        elif event['event'] == 'command_terminated' and open_commands.get(command):
            # A tool restarting itself starts the new run before ending the old one, so oldest closes first.
            row = open_commands[command].pop(0)
            row['duration_ms'] = event['ts_ms'] - row['ts_ms']
            row['termination_reason'] = event.get('termination_reason')
    return rows


def task_events(records):
    # logging_started only marks the moment of consent. Reading that screen is not task time.
    # Nor is the end review, where checking a saved model can open it again, nor answering a file's questions.
    # The first event after such a window is marked, so the window's time is never counted as a gap.
    events, reviewing, answering, outside_commands, broken = [], False, False, 0, False
    ambient = ambient_commands(records[0] if records else {})
    for record in records:
        if record.get('record') == 'review':
            reviewing = record.get('state') == 'opened'
            broken = True
        elif record.get('record') == 'file_questions':
            answering = record.get('state') == 'opened'
            broken = True
        elif record.get('record') == 'event' and record.get('event') != 'logging_started':
            if not (reviewing or answering):
                events.append({**record, 'after_break': True} if broken else record)
                broken = False
            elif record['event'] == 'command_starting' and record.get('command_id') not in ambient:
                outside_commands += 1
    return events, outside_commands


def file_answers(records):
    """Per file: the latest answers, how many times it was answered, and time spent on its questions."""
    found, opened = {}, None
    for record in records:
        kind = record.get('record')
        if kind == 'file_questions' and record.get('state') == 'opened':
            opened = record
        elif kind == 'file_questions' and opened is not None:
            entry = found.setdefault(record.get('model_id'), {'answers': 0, 'questions_time_ms': 0})
            entry['questions_time_ms'] += record['ts_ms'] - opened['ts_ms']
            opened = None
        elif kind == 'file_answers':
            entry = found.setdefault(record.get('model_id'), {'answers': 0, 'questions_time_ms': 0})
            # A later answer replaces an earlier one, and the count shows that it happened.
            entry.update({'answers': entry['answers'] + 1, 'difficulty': record.get('difficulty'), 'note': record.get('note')})
    return found


def analyse(path, codes):
    header, records = read_session(path)
    warnings = validate(path, header, records)
    events, review_commands = task_events(records)
    if review_commands:
        warnings.append(f'{os.path.basename(path)}: {review_commands} commands ran during the end review or file questions and are left out of the metrics')
    metrics, commands = [], []
    if not events:
        return metrics, commands, [], warnings + [f'{os.path.basename(path)}: no events']
    checks, answers, notes = target_checks(records), file_answers(records), []
    session = metrics_for(split_at_breaks(events), header, 'session', None, [c for group in checks.values() for c in group])
    session.update({'answers': sum(a['answers'] for a in answers.values()),
                    'questions_time_ms': sum(a['questions_time_ms'] for a in answers.values())})
    metrics.append(session)
    for model_id, visits in split_visits(events).items():
        row = metrics_for(visits, header, 'model', codes.get(model_id, model_id), checks.get(model_id, ()))
        answer = answers.get(model_id, {})
        row.update({'answers': answer.get('answers', 0), 'difficulty': answer.get('difficulty'),
                    'questions_time_ms': answer.get('questions_time_ms', 0)})
        metrics.append(row)
        if answer.get('note'):
            # Free text is kept out of metrics.csv, so it can be read and cleaned before anything is shared.
            notes.append({'session_id': header['session_id'], 'participant_code': header['participant_code'],
                          'model': row['model'], 'note': answer['note']})
    commands.extend(command_rows(events, header, codes))
    return metrics, commands, notes, warnings


FORMULA_START = re.compile(r'^\s*[=@]|^[\t\r]|^[+-]\s*[^\d\s.]')


def spreadsheet_safe(value):
    # Excel runs a cell starting like this as a formula, and logs can be edited, so nothing is trusted.
    # A leading + or - before a number is left alone. Notes keep their exact text in notes.jsonl.
    if isinstance(value, str) and FORMULA_START.search(value):
        return "'" + value
    return value


def printable(text):
    # Warnings echo values from the log, and an edited log could hold terminal control codes.
    return ''.join(c if c.isprintable() else repr(c)[1:-1] for c in str(text))


def write_csv(path, fields, rows):
    # Owner-only, like the logs: notes.csv holds free text next to participant codes.
    with open(path, 'w', newline='', encoding='utf-8', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        os.chmod(path, 0o600)
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows({k: spreadsheet_safe(v) for k, v in row.items()} for row in rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('logs', nargs='+')
    parser.add_argument('--out', required=True)
    parser.add_argument('--model-map')
    args = parser.parse_args(argv)

    codes = {}
    if args.model_map:
        with open(args.model_map, encoding='utf-8') as f:
            codes = {model_hash(name): code for name, code in json.load(f).items()}

    metrics, commands, notes, warnings, rejected = [], [], [], [], 0
    for path in args.logs:
        try:
            m, c, n, w = analyse(path, codes)
        except LogRejected as problem:
            # One bad log must not cost the results of the others. The exit code still says something was refused.
            print(f'rejected: {printable(problem)}', file=sys.stderr)
            rejected += 1
            continue
        metrics += m
        commands += c
        notes += n
        warnings += w
    if rejected == len(args.logs):
        return 2

    os.makedirs(args.out, mode=0o700, exist_ok=True)
    write_csv(os.path.join(args.out, 'metrics.csv'), METRIC_FIELDS, metrics)
    write_csv(os.path.join(args.out, 'commands.csv'), COMMAND_FIELDS, commands)
    write_csv(os.path.join(args.out, 'notes.csv'), NOTE_FIELDS, notes)
    with open(os.path.join(args.out, 'notes.jsonl'), 'w', encoding='utf-8', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        f.write(''.join(json.dumps(note, ensure_ascii=False) + '\n' for note in notes))
    for warning in warnings:
        print(f'warning: {printable(warning)}', file=sys.stderr)
    print(f'{len(args.logs) - rejected} log(s) read, {rejected} rejected: {len(metrics)} metric rows, {len(commands)} commands, {len(warnings)} warnings')
    return 2 if rejected else 0


if __name__ == '__main__':
    sys.exit(main())
