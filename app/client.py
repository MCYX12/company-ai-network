import argparse
import asyncio
import json
import uuid
import time
from .common import connect, query, request

async def main(args):
    nc = await connect(args.identity)
    try:
        body = request(args.identity, args.target or 'ag-b', action=args.action, project=args.project)
        if args.submit_task:
            body['action']='submit_task'
            body['payload']={'operation':'generate_text' if args.prompt else 'get_status','idempotency_key':args.idempotency_key or str(uuid.uuid4()),'ttl_seconds':args.ttl}
            if args.prompt:
                body['payload']['prompt']=args.prompt
        elif args.tasks:
            body.pop('target_agent_id')
            body['action']='list_tasks'
            body['payload']={'limit':args.limit}
            if args.state:body['payload']['state']=args.state
            if args.cursor:body['payload']['cursor']=args.cursor
        elif args.cancel_task:
            body.pop('target_agent_id')
            body['action']='cancel_task'
            body['payload']={'task_id':args.cancel_task}
        elif args.task_id:
            body.pop('target_agent_id')
            body['action']='get_task'
            body['payload']={'task_id':args.task_id}
        elif args.list:
            body['action'] = 'list_agents'
            body.pop('target_agent_id')
            body['payload'] = {}
            if args.capability:
                body['payload']['capability'] = args.capability
        elif args.capability:
            body.pop('target_agent_id')
            body['action'] = args.capability
            body['capability'] = args.capability
        result = await query(nc, args.identity, body)
        if args.wait and result.get('code') == 'ACCEPTED':
            task_id = result['data']['task_id']
            until = time.monotonic() + args.ttl + 5
            while time.monotonic() < until:
                poll = request(args.identity)
                poll.pop('target_agent_id')
                poll['action'] = 'get_task'
                poll['payload'] = {'task_id': task_id}
                result = await query(nc, args.identity, poll)
                if result.get('code') != 'OK' or result['data']['state'] in {'succeeded','failed','expired','cancelled'}:
                    break
                await asyncio.sleep(1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await nc.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--identity', default='ag-a')
    parser.add_argument('--target')
    from .site import defaults
    parser.add_argument('--project', default=defaults().get('project'))
    parser.add_argument('--action', choices=['get_status', 'get_project_progress'], default='get_project_progress')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--capability', choices=['get_status', 'get_project_progress'])
    parser.add_argument('--submit-task',action='store_true',help='Submit durable get_status task')
    parser.add_argument('--wait',action='store_true',help='Wait for submitted task result')
    parser.add_argument('--prompt',help='Text test prompt; requires --submit-task')
    parser.add_argument('--tasks',action='store_true',help='List own authorized tasks, newest first')
    parser.add_argument('--state',choices=['accepted','queued','dispatched','running','succeeded','failed','expired','cancelled'])
    parser.add_argument('--limit',type=int,default=20)
    parser.add_argument('--cursor',type=int)
    parser.add_argument('--cancel-task',metavar='TASK_ID',help='Cancel own task only before first dispatch')
    parser.add_argument('--task-id',help='Read a task owned by this identity')
    parser.add_argument('--idempotency-key')
    parser.add_argument('--ttl',type=int,default=60)
    args = parser.parse_args()
    if not 1 <= args.limit <= 50 or (args.cursor is not None and args.cursor < 1):
        parser.error('limit must be 1..50; cursor must be positive')
    if (args.state or args.cursor is not None or args.limit != 20) and not args.tasks:
        parser.error('--state/--cursor/--limit require --tasks')
    if args.tasks and args.target:
        parser.error('--tasks cannot be combined with --target')
    if args.wait and not args.submit_task:
        parser.error('--wait requires --submit-task')
    if args.prompt is not None and (not args.submit_task or not 1 <= len(args.prompt.strip()) <= 4000):
        parser.error('--prompt requires --submit-task and 1..4000 nonblank characters')
    if sum(bool(x) for x in [args.submit_task,args.task_id,args.cancel_task,args.tasks,(args.list or args.capability)])>1:
        parser.error('Choose one operation')
    if (args.task_id or args.cancel_task) and args.target:
        parser.error('--task-id cannot be combined with --target')
    if args.target and (args.list or args.capability):
        parser.error('--target cannot be combined with --list or --capability')
    asyncio.run(main(args))
