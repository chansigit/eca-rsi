import re

import pytest

from harness_bridge import _harness_host_tools as T


def body(result):
    return result['content'][0]['text']


def test_large_barcode_table_is_bounded_and_has_a_real_cursor(tmp_path):
    p=tmp_path/'cells.csv'
    p.write_text('cell,remove\n'+''.join(f'AA{i:016x},False\n' for i in range(81079)))
    result=body(T._read(tmp_path,{'file_path':p.name}))
    assert len(result.encode()) < T.READ_DEFAULT_BYTES+500
    assert f'next byte_offset={T.READ_DEFAULT_BYTES}' in result
    assert 'AA0000000000013cb6' not in result
    second=body(T._read(tmp_path,{'file_path':p.name,'byte_offset':T.READ_DEFAULT_BYTES,'max_bytes':100}))
    assert p.read_bytes()[T.READ_DEFAULT_BYTES:T.READ_DEFAULT_BYTES+100].decode() in second


def test_utf8_pages_reconstruct_exactly_without_splitting_characters(tmp_path):
    text='细胞\\nαβγ🙂'*30
    p=tmp_path/'utf8.txt';p.write_text(text)
    offset=0;parts=[]
    for _ in range(1000):
        result=body(T._read(tmp_path,{'file_path':p.name,'byte_offset':offset,'max_bytes':11}))
        payload=result.split('<content>\n',1)[1].rsplit('\n</content>',1)[0]
        parts.append(payload.split('\n\n[page truncated;',1)[0])
        match=re.search(r'next byte_offset=(\d+)',result)
        if not match:
            break
        new=int(match.group(1));assert new>offset;offset=new
    else:
        pytest.fail('reader did not make progress')
    assert ''.join(parts)==text


def test_unbroken_line_cannot_evade_byte_cap(tmp_path):
    p=tmp_path/'long';p.write_text('x'*100000)
    result=body(T._read(tmp_path,{'file_path':p.name,'max_bytes':10**9}))
    assert len(result)<T.READ_MAX_BYTES+500
    assert f'next byte_offset={T.READ_MAX_BYTES}' in result


@pytest.mark.parametrize('args',[{'byte_offset':-1},{'byte_offset':True},{'max_bytes':-1},{'max_bytes':'100'},{'byte_offset':10**9}])
def test_invalid_page_arguments(tmp_path,args):
    (tmp_path/'file').write_text('abc')
    assert T._read(tmp_path,{'file_path':'file',**args})['is_error']


def test_read_schema_describes_bounded_paging(tmp_path):
    from harness_bridge._harness_openai import _params_schema
    spec=T.readonly_tools(str(tmp_path),('read',))[0]
    assert set(spec.input_schema)=={'file_path','byte_offset','max_bytes'}
    assert set(_params_schema(spec)['required'])==set(spec.input_schema)
    assert 'hard maximum' in spec.description


def test_manual_offset_inside_utf8_character_is_rejected(tmp_path):
    p=tmp_path/'utf8.txt';p.write_text('细胞')
    result=T._read(tmp_path,{'file_path':p.name,'byte_offset':1})
    assert result['is_error'] and 'UTF-8 character' in body(result)


def test_read_uses_open_descriptor_size_after_truncation(tmp_path,monkeypatch):
    from pathlib import Path
    p=tmp_path/'file';p.write_text('x'*100)
    original=Path.open
    def truncated(path,*args,**kwargs):
        if path == p and args == ('rb',):
            with original(path,'wb'):
                pass
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',truncated)
    result=body(T._read(tmp_path,{'file_path':p.name}))
    assert 'bytes 0:0 of 0' in result
    assert 'next byte_offset' not in result


def test_file_mutation_during_read_is_rejected(tmp_path,monkeypatch):
    from types import SimpleNamespace
    p=tmp_path/'file';p.write_text('abc')
    original=T.os.fstat
    calls=[]
    def changed(fd):
        st=original(fd);calls.append(fd)
        return SimpleNamespace(st_size=st.st_size,st_mtime_ns=st.st_mtime_ns+len(calls))
    monkeypatch.setattr(T.os,'fstat',changed)
    result=T._read(tmp_path,{'file_path':p.name})
    assert result['is_error'] and 'changed during Read' in body(result)
