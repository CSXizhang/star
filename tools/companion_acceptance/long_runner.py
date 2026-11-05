"""Single-session mixed native-game evaluation; no model coaching or action dispatch."""
import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from runner import Runner, count, native_finished, read, rows, summarize_trace, write

INSTRUCTION = "今天先把没成熟的作物照料好，再把成熟的防风草全部收好：留三根在田边箱子里，其余五根放进出货箱。再采两棵成熟树，把田边原有的材料和本次砍树的掉落物捡好，木头也存进田边箱子；幼树和没成熟的作物留着。体力不够可以自行安排恢复，今天做完就歇着。"

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def checks(before, after):
    initial=before['harnessFixtureTargets']; final={(v['x'],v['y']):v for v in after.get('harnessFixtureTargets',[])}
    growing=[v for v in initial if not v['readyForHarvest']]; mature=[v for v in initial if v['readyForHarvest']]
    chest=after.get('acceptanceChestItems'); bag=after.get('companionItems'); shipped=after.get('acceptanceShippedItems')
    return {
      'eight_growing_crops_preserved_watered':len(growing)==8 and all(final.get((v['x'],v['y']),{}).get('hasCrop') and final[(v['x'],v['y'])].get('waterState')==1 for v in growing),
      'eight_mature_crops_harvested':len(mature)==8 and all(final.get((v['x'],v['y']),{}).get('hasCrop') is False for v in mature),
      'three_parsnips_stored':count(chest,'(O)24')==3,
      'five_parsnips_shipped':count(shipped,'(O)24')-count(before.get('acceptanceShippedItems'),'(O)24')==5,
      'no_parsnips_carried':count(bag,'(O)24')==0,
      'both_mature_trees_cut':before.get('acceptanceMatureTreesRemaining')==2 and after.get('acceptanceMatureTreesRemaining')==0,
      'sapling_preserved':before.get('acceptanceSaplingStage')==after.get('acceptanceSaplingStage')==2,
      'all_native_drops_collected':after.get('mixedGroundItems')==[] and after.get('acceptanceGroundDropRemaining') is False,
      'all_actual_wood_stored':after.get('mixedWoodCreated',0)>0 and after.get('mixedWoodLost')==0 and count(chest,'(O)388')==count(before.get('acceptanceChestItems'),'(O)388')+3+after.get('mixedWoodCreated',-999) and count(bag,'(O)388')==0 and count(shipped,'(O)388')==0,
      'observer_did_not_collect_wood':count(after.get('mixedObserverItems'),'(O)388')==count(before.get('mixedObserverItems'),'(O)388'),
      'same_game_day':all(after.get(k)==before.get(k) for k in ('year','season','day')),
      'execution_clock_not_harness_frozen':before.get('harnessTimeFrozen') is False and after.get('harnessTimeFrozen') is False,
      'native_action_finished':native_finished(after),
      'no_solid_overlaps':after.get('movementSamples',0)>0 and after.get('solidOverlaps')==[],
      'walking_frames_changed':any(k.startswith('walking-facing:') and len(set(v))>=2 for k,v in after.get('animationFrames',{}).items())}

async def main(run, timeout):
    r=Runner(run,'mixed-long',timeout,keep_open=True)
    report={'status':'setup-error','instruction':INSTRUCTION,'wallBudgetSeconds':timeout,'interventionsAfterDispatch':0,'samples':r.samples}
    ev=r.evidence; started=time.time(); sent=False
    try:
      await r.wait(lambda:(read(ev/'harness-coordination.json') or {}).get('state')=='READY_FOR_ACT','Isolated mixed fixture not ready',240)
      fixture=read(ev/'harness-fixture-startup.json')
      if not fixture or fixture.get('mode')!='mixed-long': raise RuntimeError('Not mixed-long startup fixture')
      if not Path(fixture['isolatedSavesPath']).resolve().is_relative_to(run.resolve()): raise RuntimeError('Save isolation failed')
      if len(fixture.get('nativeReachability',[]))!=19 or not all(t.get('nativePathSuccess') for t in fixture['nativeReachability']): raise RuntimeError('Post-layout native reachability missing')
      report['fixture']=fixture
      report['frozenModSha256']=sha(r.mod/'StardewAI.Companion.Mod.dll')
      report['testDriverSha256']=sha(run/'mods/StardewAI.Companion.TestDriver/StardewAI.Companion.TestDriver.dll')
      backend=read(r.mod/'config/chat-backend.json') or {}
      report['backend']={k:backend[k] for k in ('backend','effort','model') if k in backend}
      report['evaluatorSha256']=sha(Path(__file__))
      report['taskSha256']=hashlib.sha256(INSTRUCTION.encode('utf-8')).hexdigest()
      report['criteriaSourceSha256']=hashlib.sha256(__import__('inspect').getsource(checks).encode('utf-8')).hexdigest()
      saveRoot=Path(fixture['isolatedSavesPath'])
      report['saveHashesAtReady']={str(p.relative_to(saveRoot)):sha(p) for p in saveRoot.rglob('*') if p.is_file()}
      if not report['saveHashesAtReady']: raise RuntimeError('No isolated source-save hashes')
      await r.request('close')
      prep=await r.native(); report['prepared']=prep
      if len(prep.get('harnessFixtureTargets',[]))!=16 or prep.get('acceptanceMatureTreesRemaining')!=2 or prep.get('acceptanceSaplingStage')!=2 or prep.get('companionStamina')!=10 or prep.get('companionWater')!=1: raise RuntimeError('Fixed initial resources mismatch')
      deadline=time.monotonic()+120
      while True:
        ui=await r.request('snapshot')
        if ui.get('productionChatConnected') and ui.get('chatStatus') in ('Idle','Completed','Failed'): break
        if time.monotonic()>deadline: raise TimeoutError('Production bridge not ready')
        await asyncio.sleep(1)
      report['commandOffset']=len(rows(r.mod/'chat_commands.jsonl')); report['traceOffset']=len(rows(r.mod/'data/mcode-public-events.jsonl')); report['usageOffset']=len(rows(r.mod/'data/context-usage.jsonl'))
      await r.request('harness-start-clock'); before=await r.native();report['before']=before
      if before.get('harnessTimeFrozen') is not False: raise RuntimeError('Clock still frozen')
      write(ev/'long-preflight.json',report)
      await r.request('open-conversation');await r.request('text',field='_input',text=INSTRUCTION);await r.request('key',key='Enter');await r.request('close')
      sent=True; begin=time.time();report['taskStartedUtc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(begin));report['status']='running';write(ev/'long-status.json',report)
      deadline=time.monotonic()+timeout;terminal=None;stop='wall-budget'
      while time.monotonic()<deadline:
        sample=await r.native()
        terminal=next((c for c in rows(r.mod/'chat_commands.jsonl')[report['commandOffset']:] if c.get('prompt')==INSTRUCTION),None)
        status={'timestampUtc':sample.get('timestampUtc'),'elapsedSeconds':round(time.time()-begin,1),'gameTime':sample.get('timeOfDay'),'day':sample.get('day'),'stamina':sample.get('companionStamina'),'cropCount':sample.get('harnessFixtureCropCount'),'dry':sample.get('harnessFixtureDryCropCount'),'trees':sample.get('acceptanceMatureTreesRemaining'),'chest':sample.get('acceptanceChestItems'),'shipped':sample.get('acceptanceShippedItems'),'activeTask':sample.get('companionActiveTaskId'),'terminal':terminal}
        write(ev/'long-status.json',status)
        if terminal: stop='model-round-ended';break
        if any(sample.get(k)!=before.get(k) for k in ('year','season','day')):stop='game-day-ended';break
        await asyncio.sleep(2)
      report['stopReason']=stop;report['taskSeconds']=round(time.time()-begin,1);report['turn']=terminal
      await r.request('close');await r.request('harness-capture');after=await r.native();report['after']=after
      report['checks']=checks(before,after)
      report['checks']['real_mcode_round_completed']=bool(terminal and terminal.get('provider')=='mcode' and terminal.get('status')=='completed')
      trace=rows(r.mod/'data/mcode-public-events.jsonl')[report['traceOffset']:];report['traceSummary']=summarize_trace(trace)
      report['contextUsage']=rows(r.mod/'data/context-usage.jsonl')[report['usageOffset']:]
      report['status']='passed' if all(report['checks'].values()) else 'failed'
    except Exception as error:  # noqa: BLE001 - Archive unexpected harness failures before native cleanup.
      report['status']='error' if sent else 'setup-error'
      report['error']=f'{type(error).__name__}: {error}'
    finally:
      report['elapsedSeconds']=round(time.time()-started,1);report['dispatched']=sent
      write(ev/'long-report.json',report)
      write(ev/'long-trace.json',rows(r.mod/'data/mcode-public-events.jsonl')[report.get('traceOffset',0):])
      write(ev/'free-mode-control-done.json',{'success':report['status']=='passed'})
      print(json.dumps({'status':report['status'],'error':report.get('error'),'report':str(ev/'long-report.json')},ensure_ascii=False),flush=True)
    return 0 if report['status']=='passed' else 1
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run-dir',required=True,type=Path);p.add_argument('--timeout',type=int,default=1200);a=p.parse_args();raise SystemExit(asyncio.run(main(a.run_dir,a.timeout)))
