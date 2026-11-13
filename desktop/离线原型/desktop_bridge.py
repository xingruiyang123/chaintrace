import json,sys
from pathlib import Path
from service import read_input, run_data, export
from chaintrace_matcher import timestamp

def main():
    try:
        req=json.load(sys.stdin)
        action=req['action']
        if action=='run':
            data=read_input(req['path']); result=run_data(data)
            answer={'ok':True,'result':result,'input':data,
                    'timeline_times':[timestamp(e['event_time']).isoformat(timespec='microseconds') if e.get('event_time') is not None else None for e in data['events']]}
        elif action=='export':
            result=req['result']
            answer={'ok':True,'log':export(result,req['output_path'],req['input'])}
        elif action=='mixer-sample':
            # 混币模块只有一份内置虚拟样例，路径由服务端给出，前端不拼路径
            import mixer_service
            answer={'ok':True,'path':str(mixer_service.SAMPLE_PATH)}
        elif action=='mixer-run':
            # 延迟导入：algo2 出任何问题都不影响既有的核验流程
            import mixer_service
            result,dataset=mixer_service.run(req['path'])
            answer={'ok':True,'result':result,'dataset':dataset}
        elif action=='mixer-export':
            import mixer_service
            answer={'ok':True,'log':mixer_service.export(req['result'],req['output_path'],req.get('input'))}
        else: raise ValueError('不支持的操作')
    except Exception as exc: answer={'ok':False,'error':str(exc)}
    json.dump(answer,sys.stdout,ensure_ascii=False,default=str)
if __name__=='__main__': main()
