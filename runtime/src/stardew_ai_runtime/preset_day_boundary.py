"""Opt-in isolated preset day budget; native admission is the independent backstop."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from .snapshot_cache import read_cache, write_cache


class PresetDayBoundary:
    def __init__(self, run_dir):
        self.root=Path(run_dir) if run_dir else None
        self.preset_path=os.environ.get('STARDEW_AI_PRESET_GOAL_FILE')
        self.enabled=bool(self.root and self.preset_path)
        self.rows={}
        self._validated_bindings={}
        if self.enabled:
            self.path=self.root/'data/preset-day-boundary.json'
            try:
                self.rows=read_cache(self.path)
            except FileNotFoundError:
                pass
    @staticmethod
    def native_day(payload):
        world=(payload or {}).get('world') or {}
        seasons={'spring':0,'summer':1,'fall':2,'winter':3}
        try:
            return (int(world['year'])-1)*112+seasons[str(world['season']).lower()]*28+int(world['dayOfMonth'])-1
        except (KeyError,TypeError,ValueError):
            return None
    def record(self, save_id, store):
        if not self.enabled or not save_id:
            return None
        preset=json.loads(Path(self.preset_path).read_text(encoding='utf-8-sig'))
        request='preset-'+hashlib.sha256((preset['runId']+'\n'+save_id).encode()).hexdigest()[:12]
        cached=self.rows.get(request)
        if cached and cached.get('goalId'):
            # A snapshot checks this gate several times. Re-reading the entire
            # retained ledger on every identical check can starve the chat reader.
            # Cache only the verified binding, never the day/admission result;
            # any cross-process ledger replacement forces fresh validation.
            def ledger_revision():
                path=getattr(store,'state_path',None)
                try:
                    info=Path(path).stat()
                    return (info.st_mtime_ns,info.st_size,info.st_ino)
                except (OSError,TypeError):
                    return None
            revision=ledger_revision()
            binding=(save_id,cached['goalId'],revision)
            if revision is not None and self._validated_bindings.get(request)==binding:
                return cached
            # Identical goal text is not a run identity. An older checkpoint
            # can restore the previous run's active goal with the same text.
            old_goal=next((g for g in store.state(save_id).goals if g.id==cached['goalId']),None) if store else None
            if old_goal and old_goal.constraints.get('presetRequestId') not in {None,request}:
                cached={**cached,'goalId':None}
                self.rows[request]=cached
            else:
                if store:
                    store.bind_preset_main_goal(save_id,cached['goalId'],str(preset.get('goalText') or ''))
                    revision=ledger_revision()
                    if revision is not None:
                        self._validated_bindings[request]=(save_id,cached['goalId'],revision)
                return cached
        marker_path=self.root/'data/preset-goal-state.json'
        if not marker_path.is_file():
            return None  # New native save has not yet received its autostart marker.
        marker=json.loads(marker_path.read_text(encoding='utf-8-sig')).get(request,{})
        if not marker:
            return None
        maximum=int(marker.get('maxGameDays') or preset.get('maxGameDays') or 1)
        if maximum<1 or maximum>28:
            raise ValueError('invalid persisted preset day budget')
        start=marker.get('startDay')
        goal_id=marker.get('goalId')
        state=store.state(save_id) if store else None
        if state:
            root=next((r for r in state.requests if r.get('id')==request),{})
            goal_id=goal_id or root.get('goalId')
            bound=next((g for g in state.goals if g.id==goal_id),None)
            if bound and bound.constraints.get('presetRequestId') not in {None,request}:
                goal_id=None
            if not goal_id:
                # The original request may have been recorded before the goal
                # existed (or was trimmed before the binding could be read
                # back). Recover the binding from the persisted goals: the
                # preset run's goal is the active main goal matching the
                # preset runId/goalText.
                candidates=[g for g in state.goals if g.status=='active' and g.kind=='main']
                matched=next((g for g in candidates
                              if g.constraints.get('presetRequestId')==request
                              or g.constraints.get('presetRunId')==preset['runId']
                              or g.constraints.get('runId')==preset['runId']),None)
                if matched is not None:
                    goal_id=matched.id
                    # Persist the recovered binding on the request record so
                    # later record()/finalize() calls stay consistent.
                    if root:
                        root['goalId']=goal_id
            # Legacy migration uses actual native-sleep ledger, never current-day reset.
            sleeps=[e for e in map(asdict, state.executions) if e.get('operation') in {'sleep','native_answer'} and e.get('outcome')=='completed'
                    and any(t.id==e.get('task_id') and t.goal_id==goal_id for t in state.tasks)]
            if start is None:
                days=[f['startDay'] for e in sleeps for f in e.get('effects',[]) if f.get('via')=='native-sleep' and f.get('kind')=='day-advanced']
                if days:
                    start=min(days)
        row={'requestId':request,'runId':preset['runId'],'saveId':save_id,'startDay':start,'maxGameDays':maximum,'goalId':goal_id,'phase':'active' if start is not None else 'ended-unknown-start'}
        if cached:
            row={**cached,'goalId':goal_id}
        if store and row.get('goalId'):
            store.bind_preset_main_goal(save_id,row['goalId'],str(preset.get('goalText') or ''))
        self.rows[request]=row
        write_cache(self.path,self.rows)
        return row
    def blocked(self, save_id, store, payload):
        if not self.enabled:
            return False
        try:
            r=self.record(save_id,store)
            if not r:
                return True
            day=self.native_day(payload)
            return r.get('startDay') is None or day is None or day<r['startDay'] or day>=r['startDay']+r['maxGameDays'] or r.get('phase') in {'completed','cancelled','ended-error'}
        except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError):
            return True
    def finalize(self, save_id, store, payload):
        if not self.blocked(save_id,store,payload):
            return None
        r=self.record(save_id,store)
        if not r or not store:
            return None
        # Admission fails closed on missing/transient observations. That is
        # not evidence that the persisted native day budget has elapsed and
        # must never revoke the player's long-running goal.
        day = self.native_day(payload)
        start = r.get('startDay')
        if day is None or start is None or day < start:
            return None
        if day < start + r['maxGameDays'] and r.get('phase') not in {'completed', 'cancelled'}:
            return None
        r['observedBoundaryDay'] = day
        state=store.state(save_id)
        root=next((q for q in state.requests if q.get('id')==r['requestId']),{})
        goal_id=r.get('goalId') or root.get('goalId')
        if not goal_id:
            return None
        r['goalId']=goal_id
        goal=next((g for g in state.goals if g.id==goal_id),None)
        if not goal:
            return None
        if goal.status=='cancelled':
            r['phase']='cancelled'
            write_cache(self.path,self.rows)
            return r
        tasks=[t for t in state.tasks if t.goal_id==goal_id]
        sleep_ids={t.id for t in tasks if any(s.operation in {'sleep','native_answer'} for s in t.steps)}
        sleeps=[e for e in map(asdict, state.executions) if e.get('task_id') in sleep_ids and e.get('operation') in {'sleep','native_answer'} and e.get('outcome')=='completed']
        proofs=[f for e in sleeps for f in e.get('effects',[]) if f.get('via')=='native-sleep' and f.get('kind')=='day-advanced'
                and r.get('startDay') is not None and f.get('endDay',-1)>=r['startDay']+r['maxGameDays']]
        persisted=r.get('nativeSleep')
        if isinstance(persisted,dict) and persisted.get('via')=='native-sleep' and persisted.get('kind')=='day-advanced' and persisted.get('startDay') is not None and persisted.get('endDay',-1)>=r['startDay']+r['maxGameDays']:
            proofs.append(persisted)
        # DayStarted may arrive before the successful sleep receipt is committed.
        if not proofs and any(t.status=='running' and any(s.operation in {'sleep','native_answer'} for s in t.steps) for t in tasks):
            return None
        if not proofs:
            store.cancel_goal(save_id,goal_id)
            r.update(phase='cancelled',reason='Day budget ended without verified successful native sleep; no success claimed.')
        else:
            # A timed autonomous trial ends its authorization at the native saved
            # boundary. Old partial/planned intents are unfinished facts, not a
            # human cancellation of the whole run nor fabricated business success.
            try:
                retired = store.retire_budget_intents(save_id, goal_id)
            except Exception as ex:
                if "BOUNDARY_RECONCILIATION_REQUIRED" in str(ex):
                    r.update(phase='awaiting-reconciliation', reason=str(ex))
                    write_cache(self.path, self.rows)
                    return None
                raise
            r['unfinishedTaskIdsAtBoundary'] = retired
            # Ledger order is native completion order. Exclude any historical
            # over-budget actions recorded after the verified boundary sleep.
            counts={}
            owned=set(root.get('taskIds') or []) | {t.id for t in tasks}
            for event in map(asdict, state.executions):
                # Daily compaction may archive the completed task definitions.
                # Native ledger goal ownership remains stable across all days.
                if event.get('goal_id') != goal_id and event.get('task_id') not in owned:
                    continue
                for effect in event.get('effects',[]):
                    kind=effect.get('state') or effect.get('kind')
                    if kind in {'watered','harvested','planted','cleared','shipped','purchased','refilled','chopped-tree'}:
                        quantity = effect.get('count', 1)
                        if type(quantity) is int and quantity > 0:
                            counts[kind]=counts.get(kind,0)+quantity
                if event in sleeps and any(f in proofs for f in event.get('effects',[])):
                    break
            # Reload can legitimately omit the post-save sleep receipt; retain
            # its already verified persisted facts, never replay its native action.
            if counts:
                r['effectsScopeRequestId']=r['requestId']
            elif r.get('effectsScopeRequestId') == r['requestId']:
                counts=r.get('verifiedEffectsBeforeSleep') or {}
            r['verifiedEffectsBeforeSleep']=counts
            r['effectsScopeGoalId']=goal_id
            # Only a previously verified terminal may restore closure from the
            # post-save journal when the save checkpoint omits its sleep receipt.
            restore = proofs[-1] if r.get('phase') == 'completed' and persisted in proofs else None
            store.complete_goal(save_id,goal_id,verified_native_boundary=restore)
            store.revise_goal(save_id,goal_id,project={**goal.project,'completionAssessment':'verified-native-preset-day-boundary','nativeSleep':proofs[-1],'maxGameDays':r['maxGameDays']})
            r.update(phase='completed',nativeSleep=proofs[-1],reason='Verified successful native sleep/save closed the isolated day budget.')
        write_cache(self.path,self.rows)
        return r

    def reconcile(self, save_id, store, payload):
        if not self.enabled:
            return None
        r=self.record(save_id,store)
        if r and store and not self.blocked(save_id,store,payload):
            state=store.state(save_id)
            root=next((q for q in state.requests if q.get('id')==r['requestId']),{})
            goal=next((g for g in state.goals if g.id==(r.get('goalId') or root.get('goalId'))),None)
            if goal and goal.status=='completed':
                store.revise_goal(save_id,goal.id,status='active',project={'phase':'active','nextAction':'Continue the remaining configured game days; today is only a daily milestone.'})
                r.update(goalId=goal.id,phase='active',reason='Daily completion is progress only; the configured multi-day goal remains active.')
                write_cache(self.path,self.rows)
        return self.finalize(save_id,store,payload)
