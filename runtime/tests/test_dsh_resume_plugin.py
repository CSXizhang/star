"""Execute the shipped compatibility code against strict official API doubles."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from stardew_ai_runtime import agent_backends


@pytest.mark.parametrize("scenario", ["resume", "not-found", "owned", "corrupt"])
def test_resume_only_creates_on_exact_persistence_not_found(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required by dsh compatibility plugin")
    plugin = Path(agent_backends.__file__).with_name("dsh_resume_plugin.mjs")
    script = r"""
import vm from 'node:vm';
import fs from 'node:fs';
class NotFound extends Error {}
class Base {
  async initialize() { return {serverInfo:{name:'deepseek-harness-sdk-runtime'}}; }
}
const exportsByName = {
  'node:module': {createRequire:()=>null},
  '@deepseek-ai/dsh-sdk-jsonrpc-server': {HarnessSdkJsonRpcServer:Base},
  '@deepseek-ai/dsh-sdk-protocol': {JsonRpcLineTransport:class {}},
  '@deepseek-ai/dsh-session-persistence': {SessionPersistenceNotFoundError:NotFound},
};
const m = new vm.SourceTextModule(fs.readFileSync(process.argv[1], 'utf8'));
await m.link(name => {
  const values = exportsByName[name];
  return new vm.SyntheticModule(Object.keys(values), function () {
    for (const [key,value] of Object.entries(values)) this.setExport(key,value);
  });
});
await m.evaluate();
const calls = [];
const original = new Error(process.argv[2]);
const state = {provider:'deepseek-official',model:'deepseek-flash',reasoningEffort:'low',
  maxTokens:8192,cwd:'fixture',sessions:new Map(),ctx:{agents:{
    async resume(options) {
      calls.push(['resume',options]);
      if(process.argv[2]==='not-found') throw new NotFound();
      if(process.argv[2]!=='resume') throw original;
      return {identity:options.resumeSessionId};
    },
    async create(options) { calls.push(['create',options]); return {identity:options.sessionId}; },
  }}};
let errorPreserved = false;
try { await m.namespace.ResumableSdkServer.prototype.createSession.call(state,'same-session'); }
catch (error) { errorPreserved = error === original; }
const handshake = await m.namespace.ResumableSdkServer.prototype.initialize.call(state,{});
console.log(JSON.stringify({calls,errorPreserved,sessionCount:state.sessions.size,handshake}));
"""
    completed = subprocess.run([node, "--experimental-vm-modules", "--input-type=module", "-e", script,
                                str(plugin), scenario], capture_output=True, text=True, check=True)
    result = json.loads(completed.stdout)
    assert result["calls"][0] == ["resume", {"resumeSessionId": "same-session", "agentOptions": {
        "provider": "deepseek-official", "model": "deepseek-flash", "reasoningEffort": "low", "maxTokens": 8192}}]
    assert [call[0] for call in result["calls"]] == (["resume", "create"] if scenario == "not-found" else ["resume"])
    assert result["errorPreserved"] is (scenario in {"owned", "corrupt"})
    assert result["sessionCount"] == (0 if scenario in {"owned", "corrupt"} else 1)
    assert result["handshake"]["serverInfo"]["stardewSessionResume"] == 1
