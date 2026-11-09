import json,sys
from pathlib import Path
from service import read_input, run_data, export
from chaintrace_matcher import timestamp

def main():
    try:
        req=json.load(sys.stdin)
        if req['action']=='run':
            data=read_input(req['path']); result=run_data(data)
            answer={'ok':True,'result':result,'input':data,
                    'timeline_times':[timestamp(e['event_time']).isoformat(timespec='microseconds') if e.get('event_time') is not None else None for e in data['events']]}
        elif req['action']=='export':
            result=req['result']
            answer={'ok':True,'log':export(result,req['output_path'],req['input'])}
        else: raise ValueError('不支持的操作')
    except Exception as exc: answer={'ok':False,'error':str(exc)}
    json.dump(answer,sys.stdout,ensure_ascii=False,default=str)
if __name__=='__main__': main()
