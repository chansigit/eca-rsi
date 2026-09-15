"""Return registered-tool argument errors through the accepted tool-result boundary."""
import json
from pathlib import Path

from . import agent_session as session
from .warm_pool.state import digest, read, save, status, submit


def reject_arguments(session_ref, reply_path, index, previous, message):
    spec = session.verified(session_ref)['spec']
    reply = session.turn_reply(session_ref, reply_path)
    call = reply['calls'][index]
    tool = next(t for t in spec['tools'] if t['name'] == call['name'])
    state = spec.get('tool_state')
    result = None
    if previous:
        request = read(Path(spec['pool_root'])/'requests'/previous/'request.json')
        prior_tool = next(t for t in spec['tools'] if t['name'] == request['spec']['operation_id'])
        current = status(spec['pool_root'], previous)
        if current['state'] != 'succeeded':
            raise ValueError('Previous tool result is not accepted')
        expected = Path(spec['pool_root'])/'requests'/previous/current['attempt_id']/'outputs'/prior_tool['result_file']
        output = next(o for o in current['receipt']['outputs'] if Path(o['path']) == expected)
        result = session.verified({k:output[k] for k in ('path','sha256')})
    else:
        context = read(Path(reply_path).parent/'request.json')['spec'].get('context')
        if context:
            result = session.verified(session.verified(context)['results'][-1]['output'])
    if state is not None and result is not None:
        state = result['state']
    if state is not None:
        session.verified(state)
    request_id = spec['session_id']+'.tool-'+digest([Path(reply_path).parent.name,call['call_id']])[:16]
    directory = Path(spec['output_root'])/request_id
    directory.mkdir(mode=0o700, exist_ok=True)
    response = dict(is_error=True, content=('Invalid arguments for '+call['name']+': '+message+
        '. Correct the arguments and call the registered tool again. Expected schema: '+json.dumps(tool['parameters']))[:16000])
    if state is not None:
        response['state'] = state
    packet = session.immutable(directory/'argument-rejection.json',dict(response=response, output=tool['result_file']))
    submit(spec['pool_root'],dict(request_id=request_id,operation_id=tool['name'],
        args=['-m','ecarsi.agent_tool_errors',packet['path']],cpus=1,memory_mb=min(tool['memory_mb'],128),
        timeout_seconds=min(tool['timeout_seconds'],30),outputs=[tool['result_file']],
        inputs=[packet,session.reference(reply_path),session.reference(Path(__file__)),*([state] if state else [])],
        trace={'workflow_id':'agent/'+spec['session_id'],'dataset_id':spec['dataset_id'],
               'unit_id':tool['name'],**spec.get('trace',{}),'depends_on':[previous or Path(reply_path).parent.name]}))
    return dict(request_id=request_id,result_file=tool['result_file'],index=index)


def write_rejection(packet_path):
    packet = read(packet_path)
    output = Path(packet['output'])
    if output.is_absolute() or '..' in output.parts:
        raise ValueError('Tool output must stay in the assigned directory')
    output.parent.mkdir(parents=True,exist_ok=True)
    save(output,packet['response'])


if __name__ == '__main__':
    import sys
    write_rejection(sys.argv[1])
