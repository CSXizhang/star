"""One initial player-mode goal; observation only after normal F9 handoff and chat."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

from runner import Runner, provider_trace, read, rows, verify_agy_binding, write

ROOT=Path(__file__).resolve().parents[2]

def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def total(items, item):
    return sum(v.get('stack',v.get('count',0)) for v in items or [] if v.get('id',v.get('itemId'))==item)

def native_preflight(fixture, sample, spec):
    """Validate live engine data, never rely on a remembered crop schedule."""
    def get(d,key,default=None):
        return next((v for k,v in d.items() if k.casefold()==key.casefold()),default)
    errors=[];economics=spec['expectedNativeEconomics'];m=sample.get('multiDay') or {}
    crop=fixture.get('cropData') or {};phases=crop.get('phaseDays') or []
    growth=sum(phases[:-1]) if len(phases)>=2 and all(isinstance(x,int) and x>=0 for x in phases) and phases[-1]>=9999 else None
    shop=fixture.get('nativeShop') or {};seeds=[x for x in get(shop,'items',[]) if get(x,'itemId')==economics['seedItemId']]
    if fixture.get('mode')!='multi-day' or fixture.get('actorMode')!='player':errors.append('wrong fixture/actor mode')
    if crop.get('seedId')!=economics['seedItemId'] or crop.get('harvestId')!=economics['harvestItemId']:errors.append('crop identities differ')
    if crop.get('normalHarvestSellPrice')!=economics['normalHarvestPrice'] or growth!=economics['growthDays']:errors.append('native crop economics/growth differ')
    if len(seeds)!=1 or get(seeds[0],'price')!=economics['seedPrice']:errors.append('live SeedShop price missing or differs')
    elif not get(seeds[0],'isInfiniteStock',False) and get(seeds[0],'stock',0)<spec['criteria']['minimumPurchasedSeedsPerCycle']:errors.append('live SeedShop stock insufficient')
    if m.get('money')!=spec['initial']['money'] or fixture.get('initialMoney')!=spec['initial']['money']:errors.append('initial money differs')
    inventory=m.get('inventory') or []
    if not inventory or any(not str(x.get('id','')).startswith(('(T)','(W)')) for x in inventory):errors.append('initial inventory contains non-tool resources or is missing')
    if not m.get('chests'):errors.append('initial native chest missing')
    if m.get('crops')!=[] or m.get('shippedItems')!=[] or any(x.get('items') for x in m.get('chests',[])):errors.append('initial crops/shipping/chests are not empty')
    protected=fixture.get('protectedTrees') or [];trees={x.get('identity'):x for x in m.get('trees',[])}
    if len(protected)!=spec['criteria']['protectedTreeCount'] or any(x.get('identity') not in trees or trees[x['identity']].get('stump') or trees[x['identity']].get('stage')!=2 for x in protected):errors.append('protected tree identities/stages differ')
    if len([x for x in m.get('trees',[]) if x.get('stage',0)>=5 and not x.get('stump')])!=6:errors.append('initial six mature trees missing')
    paths=fixture.get('nativeReachability') or []
    if len(fixture.get('plots',[]))<spec['criteria']['minimumPlantsPerCycle'] or not paths or not all(x.get('nativePathSuccess') is True for x in paths):errors.append('native initial plots/routes unavailable')
    if m.get('harnessTimeFrozen') is not False:errors.append('harness clock frozen')
    return {'passed':not errors,'errors':errors,'observedGrowthDays':growth,'observedSeedObjectBasePrice':crop.get('seedBasePrice'),'observedSeedShopPrice':get(seeds[0],'price') if len(seeds)==1 else None,'rawCropData':crop,'rawNativeShop':shop}

def terminal_candidate(sample,spec):
    """Numerical candidate only; does not prove two complete crop cycles."""
    m=sample.get('multiDay') or {};criteria=spec['criteria']
    stored=[x for chest in m.get('chests',[]) for x in chest.get('items',[])]
    return (m.get('money',-1)>=criteria['minimumMoney']
      and total(stored,'(O)24')>=criteria['minimumStoredParsnips']
      and total(stored,'(O)388')>=criteria['minimumStoredWood']
      and sample.get('day',0)>=criteria['earliestCompletionDay'])

class MultiDayRunner:
    def __init__(self,run,spec_path,timeout):
        self.run=Path(run).resolve();self.spec_path=Path(spec_path).resolve();self.spec=read(self.spec_path)
        if not self.run.is_relative_to(ROOT/'.test-runs'):raise ValueError('Only an isolated .test-runs directory is permitted')
        self.r=Runner(self.run,'multi-day',timeout,keep_open=True);self.timeout=timeout;self.ev=self.r.evidence
        self.report={'scope':'autonomous multi-day player run','status':'preflight','interventionsAfterDispatch':0,'task':self.spec['instruction'],'wallBudgetSeconds':timeout,'samples':[]}
    def reviewer_stop(self):
        request = read(self.ev / "reviewer-stop.json")
        if not isinstance(request, dict) or request.get("runId") != self.run.name:
            return None
        reason = request.get("reason")
        if request.get("reviewerRequestedStop") is not True or not isinstance(reason, str) or not reason.strip():
            return None
        self.report["reviewerRequestedStop"] = True
        self.report["reviewerStopRequest"] = request
        return reason

    async def native(self):
        # This sampler accepts a genuine player actor; no companion-only assertion.
        label='multi-'+uuid.uuid4().hex[:12];p=self.ev/'free-mode-native-snapshot-request.txt'
        await self.r.wait(lambda:not p.exists(),'Native sample request remains pending',45)
        tmp=p.with_suffix('.tmp');tmp.write_text(label,encoding='utf-8');tmp.replace(p)
        value=await self.r.wait(lambda:read(self.ev/f'free-mode-native-{label}.json'),'No native player sample',45)
        if value.get('runId')!=self.run.name:raise RuntimeError('Sample run identity mismatch')
        return value
    async def execute(self):
        sent=False;start=time.time();offset=0
        try:
            await self.r.wait(lambda:(read(self.ev/'harness-coordination.json') or {}).get('state')=='READY_FOR_ACT','No isolated READY fixture',240)
            cfg=read(self.run/'config/test-session.json') or {}
            if cfg.get('runId')!=self.run.name or cfg.get('scenario')!='free-mode-control':raise RuntimeError('Unexpected harness configuration')
            save=Path(cfg['isolatedSavesPath']).resolve()
            if not save.is_relative_to(self.run):raise RuntimeError('Save root escaped run')
            savehash={str(p.relative_to(save)):sha(p) for p in save.rglob('*') if p.is_file()}
            if not savehash:raise RuntimeError('No isolated source save hashes')
            before=await self.native();self.report['before']=before
            if (before.get('year'),before.get('season'),before.get('day'))!=(1,'spring',1):raise RuntimeError('Expected genuine spring 1; no clock/date changes allowed')
            if not isinstance(before.get('multiDay'),dict):raise TypeError('Missing multiDay native observation')
            ui=await self.r.request('snapshot')
            if ui.get('actorMode')!='player' or ui.get('playerAuthorityAgentOwns') is not False:raise RuntimeError('Expected player mode awaiting explicit handoff')
            self.report['agyBindingCheck']=await verify_agy_binding(self.run,self.r.mod,before)
            fixture=read(self.ev/'harness-fixture-startup.json') or {}
            self.report['fixture']=fixture
            validation=native_preflight(fixture,before,self.spec);self.report['nativePreflight']=validation
            if not validation['passed']:
                write(self.ev/'multi-day-preflight.json',self.report)
                raise RuntimeError('Native preflight rejected: '+'; '.join(validation['errors']))
            if before['multiDay'].get('money')!=self.spec['initial']['money']:raise RuntimeError('Initial money differs from frozen specification')
            if any(total(before['multiDay'].get('inventory'),x)>0 for x in ('(O)472','(O)24','(O)388')):raise RuntimeError('Unexpected initial seeds/crops/wood')
            self.report.update({'specSha256':sha(self.spec_path),'evaluatorSha256':sha(__file__),'saveHashes':savehash,'productionDllSha256':sha(self.r.mod/'StardewAI.Companion.Mod.dll'),'testDriverSha256':sha(self.run/'mods/StardewAI.Companion.TestDriver/StardewAI.Companion.TestDriver.dll'),'initialUi':ui})
            write(self.ev/'multi-day-preflight.json',self.report)
            deadline=time.monotonic()+120
            while not ui.get('productionChatConnected') or ui.get('chatStatus') not in ('Idle','Completed','Failed'):
                if time.monotonic()>deadline:raise TimeoutError('Production bridge not ready')
                await asyncio.sleep(1);ui=await self.r.request('snapshot')
            # Only initial, explicit human-style F9 handoff; never repeat after dispatch.
            await self.r.request('close');await self.r.request('takeover-key');ui=await self.r.request('snapshot')
            if ui.get('playerAuthorityAgentOwns') is not True:raise RuntimeError('Normal F9 handoff did not grant AI authority')
            self.report['handoff']=ui;offset=len(rows(self.r.mod/'chat_commands.jsonl'));self.report['traceOffset']=len(provider_trace(self.r.mod));self.report['usageOffset']=len(rows(self.r.mod/'data/context-usage.jsonl'))
            await self.r.request('open-conversation')
            chat=await self.r.request('snapshot');chat_type=chat.get('menuType')
            if chat_type!='StardewAI.Companion.Mod.Menus.CompanionConversationMenu':raise RuntimeError('Initial conversation menu identity differs')
            await self.r.request('text',field='_input',text=self.spec['instruction'])
            await self.r.request('key',key='Enter',expectedMenuType=chat_type)
            sent=True
            # Same-frame TestDriver guard must reject any newly appeared native menu.
            try:await self.r.request('key',key='Escape',expectedMenuType=chat_type)
            except RuntimeError as error:self.report['initialChatCloseSkipped']={'error':str(error),'reason':'Never close a replacement menu'}
            sent=True;started=time.time();self.report.update({'status':'running','taskStartedUtc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(started))});write(self.ev/'multi-day-status.json',self.report)
            deadline=time.monotonic()+self.timeout;stop='wall-budget';last_error=None
            while time.monotonic()<deadline:
                reviewer_reason = self.reviewer_stop()
                if reviewer_reason is not None:
                    stop = reviewer_reason
                    break
                try:sample=await self.native()
                except (RuntimeError,TimeoutError) as error:
                    # Native overnight/fade can delay observation. No input, sleep or repair is sent.
                    last_error={'at':time.time(),'error':str(error)};self.report.setdefault('samplingErrors',[]).append(last_error)
                    reviewer_reason = self.reviewer_stop()
                    if reviewer_reason is not None:
                        stop = reviewer_reason
                        break
                    await asyncio.sleep(5);continue
                self.report['samples'].append(sample);m=sample.get('multiDay') or {}
                commands=rows(self.r.mod/'chat_commands.jsonl')[offset:]
                sessions=sorted(set((read(self.r.mod/'chat_sessions.json') or {}).values()))
                timeline=self.report.setdefault('providerSessionTimeline',[])
                if not timeline or timeline[-1]['sessions']!=sessions:timeline.append({'at':sample.get('timestampUtc'),'sessions':sessions})
                status={'at':sample.get('timestampUtc'),'elapsedSeconds':round(time.time()-started,1),'date':{k:sample.get(k) for k in ('year','season','day','timeOfDay')},'multiDay':m,'numericalCandidate':terminal_candidate(sample,self.spec),'completedRounds':len(commands),'providerSessions':sessions,'lastSamplingError':last_error}
                write(self.ev/'multi-day-status.json',status)
                with (self.ev/'multi-day-samples.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(sample,ensure_ascii=False)+'\n')
                if len(sessions)>1:stop='session-continuity-violation';break
                if m.get('harnessTimeFrozen') is not False:stop='harness-clock-contract-violated';break
                if m.get('agentOwns') is False:self.report.setdefault('authorityReleasedAt',sample.get('timestampUtc'))
                if (sample.get('year'),sample.get('season'))!=(1,'spring') or sample.get('day',99)>self.spec['criteria']['deadlineDay']:stop='game-deadline';break
                if sample.get('day')==self.spec['criteria']['deadlineDay'] and sample.get('timeOfDay',0)>=self.spec['criteria']['deadlineTime']:stop='specified-morning-deadline';break
                if terminal_candidate(sample,self.spec):self.report.setdefault('firstNumericalCandidateAt',sample.get('timestampUtc'))
                # Ordinary round completion is evidence, not permission to re-send or reset.
                await asyncio.sleep(1 if sample.get('day')==self.spec['criteria']['deadlineDay'] else 10)
            self.report.update({'status':'finished-awaiting-independent-ledger-review','stopReason':stop,'taskSeconds':round(time.time()-started,1),'after':self.report['samples'][-1] if self.report['samples'] else self.report.get('before'),'rounds':rows(self.r.mod/'chat_commands.jsonl')[offset:],'contextUsage':rows(self.r.mod/'data/context-usage.jsonl')[self.report['usageOffset']:]})
        except Exception as error:  # noqa: BLE001 - Preserve failure evidence before guaranteed native cleanup.
            self.report.update({'status':'setup-error' if not sent else 'error','error':f'{type(error).__name__}: {error}'})
        finally:
            reviewer_reason = self.reviewer_stop()
            if reviewer_reason is not None:
                self.report.update({"status": "reviewer-stopped", "stopReason": reviewer_reason,
                                    "after": self.report["samples"][-1] if self.report["samples"] else self.report.get("before"),
                                    "rounds": rows(self.r.mod / "chat_commands.jsonl")[offset:],
                                    "contextUsage": rows(self.r.mod / "data/context-usage.jsonl")[self.report.get("usageOffset", 0):]})
            ledger=rows(self.ev/'multi-day-ledger.jsonl')
            self.report['nativeLedgerEvidence']={'path':'multi-day-ledger.jsonl','sha256':sha(self.ev/'multi-day-ledger.jsonl') if (self.ev/'multi-day-ledger.jsonl').exists() else None,'events':len(ledger),'eventTypes':{k:sum(x.get('type')==k for x in ledger) for k in sorted({str(x.get('type')) for x in ledger})}}
            self.report.update({'dispatched':sent,'elapsedSeconds':round(time.time()-start,1)})
            write(self.ev/'multi-day-report.json',self.report)
            write(self.ev/'multi-day-trace.json',provider_trace(self.r.mod)[self.report.get('traceOffset',0):])
            # Dedicated completion channel cancels the owned execution before native shutdown.
            write(self.ev/'free-mode-control-done.json',{'success':not self.report.get('reviewerRequestedStop') and self.report.get('stopReason')=='specified-morning-deadline','taskSuccess':None,'taskJudgmentPending':True,'monitorStopped':True,'reason':self.report.get('stopReason',self.report.get('error'))})
            print(json.dumps({'status':self.report['status'],'dispatched':sent,'report':str(self.ev/'multi-day-report.json')},ensure_ascii=False),flush=True)
        if self.report.get("reviewerRequestedStop"):
            return 2
        return 0 if self.report.get('stopReason')=='specified-morning-deadline' else 1

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    p=argparse.ArgumentParser();p.add_argument('--run-dir',type=Path,required=True);p.add_argument('--spec-file',type=Path,required=True);p.add_argument('--timeout',type=int,default=7200);a=p.parse_args()
    raise SystemExit(asyncio.run(MultiDayRunner(a.run_dir,a.spec_file,a.timeout).execute()))
