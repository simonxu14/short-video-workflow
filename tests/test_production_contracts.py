"""v2.4只读制作合同回归；媒体像素仍由实际回看核验，不伪称自动识别。"""
import json
import wave
from pathlib import Path
import pytest
from test_flow import F, proj, run, ffmpeg


def manifest(proj, data):
    p = proj / 'contract.json'
    p.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return str(p)


def timing(proj):
    with wave.open(str(proj / 'voice.wav'), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b'\0\0' * (8000 * 24))
    (proj / 'voice.txt').write_text('自然讲故事。', encoding='utf-8')
    return {'target_seconds': 30, 'source_audio': 'voice.wav', 'frozen_text': 'voice.txt',
            'voice': 'series_voice', 'rate': '+40%', 'lead_seconds': .5, 'tail_seconds': 1.3,
            'added_pauses': [{'after_source_second': 15, 'seconds': 3.2, 'reason': '完整动作留白'}]}


@ffmpeg
def test_actual_short_voice_fits_action_budget(proj):
    code, r = run('timing-check', '--manifest', manifest(proj, timing(proj)))
    assert code == 0 and r['source_seconds'] == 24 and r['used_seconds'] == 29
    assert r['remaining_seconds'] == 1 and r['source_sha256']


@ffmpeg
@pytest.mark.parametrize('problem', ['over', 'duplicate', 'negative', 'nan', 'beyond'])
def test_invalid_timing_budget(proj, problem):
    m = timing(proj)
    if problem == 'over': m['target_seconds'] = 28
    if problem == 'duplicate': m['added_pauses'] *= 2
    if problem == 'negative': m['tail_seconds'] = -1
    if problem == 'nan': m['tail_seconds'] = float('nan')
    if problem == 'beyond': m['added_pauses'][0]['after_source_second'] = 25
    assert run('timing-check', '--manifest', manifest(proj, m))[0] == 2


def revision(proj, monkeypatch):
    for name in ('before.mp4', 'after.mp4', 'text.txt', 'timeline.json', 'style.json', 'voice.wav', 'ai.png', 'series.png', 'review.png'):
        (proj / name).write_bytes(name.encode())
    monkeypatch.setattr(F, '_ffprobe', lambda p: {'format': {'duration': '30'}, 'streams': [
        {'codec_type': 'video', 'width': 720, 'height': 1280, 'r_frame_rate': '24/1'}, {'codec_type': 'audio'}]})
    shared = {'frozen_text': 'text.txt', 'subtitle_timeline': 'timeline.json', 'subtitle_style': 'style.json', 'voice_source': 'voice.wav',
              'layers': {'ai_label': {'asset': 'ai.png', 'coverage': [{'start': 0, 'end': 30}]},
                         'series_label': {'asset': 'series.png', 'coverage': [{'start': 0, 'end': 30}]}}}
    shared['frozen_hashes'] = {k: F.sha256_file(proj / shared[k]) for k in ('frozen_text', 'subtitle_timeline', 'subtitle_style', 'voice_source')}
    for layer in shared['layers'].values(): layer['sha256'] = F.sha256_file(proj / layer['asset'])
    return {'before': {**shared, 'master': 'before.mp4'}, 'after': {**json.loads(json.dumps(shared)), 'master': 'after.mp4'},
            'export_review': {'master_sha256': F.sha256_file(proj / 'after.mp4'),
                **{k: {'passed': True, 'evidence': 'review.png'} for k in ('subtitle_text_and_style', 'labels_visible', 'voice_complete', 'protected_actions')}}}


def test_revision_consistent_not_pixel_verified(proj, monkeypatch):
    code, r = run('revision-check', '--manifest', manifest(proj, revision(proj, monkeypatch)))
    assert code == 0 and r['manifest_consistent'] and not r['pixel_verification_by_script']


@pytest.mark.parametrize('problem', ['gap', 'style', 'text', 'timeline', 'voice', 'unbound', 'unreviewed', 'missing_evidence', 'spec', 'duration'])
def test_revision_invariant_change_and_layer_gaps(proj, monkeypatch, problem):
    m = revision(proj, monkeypatch)
    if problem == 'gap':
        m['after']['layers']['ai_label']['coverage'] = [{'start': 0, 'end': 7.75}, {'start': 9.75, 'end': 30}]
    elif problem in ('style', 'text', 'timeline', 'voice'):
        key = {'style': 'subtitle_style', 'text': 'frozen_text', 'timeline': 'subtitle_timeline', 'voice': 'voice_source'}[problem]
        (proj / 'changed.txt').write_text('changed', encoding='utf-8'); m['after'][key] = 'changed.txt'
    elif problem == 'unbound': m['export_review']['master_sha256'] = 'old'
    elif problem == 'unreviewed': m['export_review']['labels_visible']['passed'] = False
    elif problem == 'missing_evidence': m['export_review']['voice_complete']['evidence'] = 'missing.png'
    else:
        original = F._ffprobe
        def probe(p):
            data = original(p)
            if p.name == 'after.mp4':
                if problem == 'spec': data['streams'][0]['width'] = 640
                else: data['format']['duration'] = '31'
            return data
        monkeypatch.setattr(F, '_ffprobe', probe)
    assert run('revision-check', '--manifest', manifest(proj, m))[0] == 2


def delivery():
    return {'origin': 'mediakit', 'original_ref': 'https://example.test/result.mp4', 'delivery_gate': 'mediakit_link_choice',
            'agent_hint': {'type': 'mediakit_artifact_delivery', 'arguments': {'questions': [{'question_id': 'link_choice'}]}},
            'choice_status': 'timeout', 'delivery_status': 'pending'}


def test_timeout_never_defaults_or_bypasses_gate(proj):
    m = delivery(); path = manifest(proj, m); before = Path(path).read_bytes()
    code, r = run('delivery-next', '--manifest', path)
    assert code == 0 and r['next'] == 'resume_same_link_choice_once' and not r['can_preview']
    assert 'ref' not in r and Path(path).read_bytes() == before
    m['origin'] = 'local_export'; m['original_ref'] = 'local.mp4'; (proj / 'local.mp4').write_bytes(b'copy')
    assert run('delivery-next', '--manifest', manifest(proj, m))[0] == 2
    m = delivery(); m['delivery_gate'] = 'none'
    assert run('delivery-next', '--manifest', manifest(proj, m))[0] == 2


@pytest.mark.parametrize('choice', ['keep_24h', 'transfer_7d'])
def test_choice_evidence_and_same_result(proj, choice):
    m = delivery(); m['link_choice'] = choice
    assert run('delivery-next', '--manifest', manifest(proj, m))[0] == 2
    (proj / 'choice.json').write_text('real tool confirmation', encoding='utf-8'); m['choice_evidence'] = 'choice.json'
    if choice == 'transfer_7d':
        r = run('delivery-next', '--manifest', manifest(proj, m))[1]
        assert r['next'] == 'transfer_same_artifact_then_deliver'
        m['transferred_ref'] = 'https://example.test/transferred.mp4'
    code, r = run('delivery-next', '--manifest', manifest(proj, m))
    assert code == 0 and r['can_preview'] and not r['publication_authorized']
    assert r['ref'] == (m.get('transferred_ref') or m['original_ref'])


@pytest.mark.parametrize('origin', ['generation_tool', 'local_export'])
def test_no_gate_deliver_now_no_duplicate(proj, origin):
    (proj / 'local.mp4').write_bytes(b'render')
    m = {'origin': origin, 'original_ref': 'local.mp4' if origin == 'local_export' else 'https://example.test/raw.mp4', 'delivery_gate': 'none'}
    assert run('delivery-next', '--manifest', manifest(proj, m))[1]['can_preview']
    m['delivery_status'] = 'delivered'
    r = run('delivery-next', '--manifest', manifest(proj, m))[1]
    assert r['next'] == 'archive_only_no_redelivery' and not r['can_preview']


@pytest.mark.parametrize('asset', ['text.txt', 'ai.png'])
def test_same_path_overwrite_does_not_bypass_frozen_hash(proj, monkeypatch, asset):
    m = revision(proj, monkeypatch)
    (proj / asset).write_bytes(b'overwritten in place')
    assert run('revision-check', '--manifest', manifest(proj, m))[0] == 2


@ffmpeg
def test_budget_integrated_with_gen_check(proj):
    from test_flow import plan
    m = timing(proj); path = manifest(proj, m)
    pp = plan(proj / 'plan.json', _timing_manifest=Path(path).name)
    assert run('gen-check', '--plan', pp)[0] == 2  # 29秒计划不能挂30秒预算
    m['target_seconds'] = 29; manifest(proj, m)
    assert run('gen-check', '--plan', pp)[0] == 0


def test_revision_contract_is_enforced_by_pack_check(proj, monkeypatch):
    from test_flow import _day, _write_pack
    day, pk = _day(proj, platforms=('douyin',))
    m = revision(proj, monkeypatch); path = manifest(proj, m)
    pk['_revision_manifest'] = str(path); _write_pack(day, pk)
    code, r = run('pack-check', '--day-dir', str(day))
    assert code == 2 and '当前发布母版' in r['error']
    m['after']['layers']['ai_label']['coverage'] = [{'start': 0, 'end': 7.75}, {'start': 9.75, 'end': 30}]
    manifest(proj, m)
    code, r = run('pack-check', '--day-dir', str(day))
    assert code == 2 and '覆盖缺口' in r['error']


@ffmpeg
def test_revision_real_metadata_probe(proj, monkeypatch):
    import subprocess
    m = revision(proj, monkeypatch)
    monkeypatch.undo()
    for file in ('before.mp4', 'after.mp4'):
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=s=72x128:d=1:r=24',
                        '-f', 'lavfi', '-i', 'sine=duration=1', '-shortest', '-c:v', 'libx264', '-c:a', 'aac', str(proj / file)], check=True)
    for section in ('before', 'after'):
        for layer in m[section]['layers'].values(): layer['coverage'] = [{'start': 0, 'end': 1}]
    m['export_review']['master_sha256'] = F.sha256_file(proj / 'after.mp4')
    code, r = run('revision-check', '--manifest', manifest(proj, m))
    assert code == 0 and abs(r['duration_seconds'] - 1) < .08

