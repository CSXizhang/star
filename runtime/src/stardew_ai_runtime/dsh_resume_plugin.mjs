// Game-local compatibility for dsh SDK 0.1.5-rc.2. The official wire server
// creates sessions but does not resume them after its process has restarted.
// Retain its protocol/event implementation and use the official resume API.
import { createRequire } from 'node:module';
import { HarnessSdkJsonRpcServer } from '@deepseek-ai/dsh-sdk-jsonrpc-server';
import { JsonRpcLineTransport } from '@deepseek-ai/dsh-sdk-protocol';
import { SessionPersistenceNotFoundError } from '@deepseek-ai/dsh-session-persistence';

export const name = 'stardew-sdk-resume';
export const inject = ['agents'];

export class ResumableSdkServer extends HarnessSdkJsonRpcServer {
  async initialize(params) {
    const result = await super.initialize(params);
    return { ...result, serverInfo: { ...result.serverInfo, stardewSessionResume: 1 } };
  }

  async createSession(sessionId) {
    const agentOptions = {
      provider: this.provider, model: this.model,
      ...(this.reasoningEffort === undefined ? {} : { reasoningEffort: this.reasoningEffort }),
      ...(this.maxTokens === undefined ? {} : { maxTokens: this.maxTokens }),
    };
    let handle;
    try {
      handle = await this.ctx.agents.resume({ resumeSessionId: sessionId, agentOptions });
    } catch (error) {
      // Ownership, corrupt logs and unsupported formats must remain failures.
      // A new session is only legal when persistence explicitly says not found.
      if (!(error instanceof SessionPersistenceNotFoundError)) throw error;
      handle = await this.ctx.agents.create({ sessionId, meta: { cwd: this.cwd }, agentOptions });
    }
    const record = { handle };
    this.sessions.set(sessionId, record);
    return record;
  }
}

export function apply(ctx) {
  const require = createRequire(import.meta.url);
  const version = require('@deepseek-ai/dsh-sdk-jsonrpc-server/package.json').version;
  // These ordinary JS fields/methods are compatibility internals, not a promise
  // made by future SDK releases. Refuse unknown versions instead of losing data.
  if (version !== '0.1.5-rc.2' ||
      typeof HarnessSdkJsonRpcServer.prototype.createSession !== 'function') {
    throw new Error('DSH_RESUME_PROTOCOL_UNSUPPORTED');
  }
  const transport = new JsonRpcLineTransport(process.stdin, process.stdout);
  const server = new ResumableSdkServer(ctx, transport, {});
  if (!(server.sessions instanceof Map) || typeof ctx.agents.resume !== 'function') {
    throw new Error('DSH_RESUME_PROTOCOL_UNSUPPORTED');
  }
  const rootFiber = ctx.root.fiber;
  let exitTask;
  transport.onRequest(async (method, params) => {
    if (method === 'initialize') await ctx.get('loader')?.await();
    const result = await server.handleRequest(method, params);
    if (method === 'shutdown') setImmediate(() => {
      exitTask ??= (async () => {
        await Promise.allSettled([Promise.resolve().then(() => transport.flush())]);
        await Promise.allSettled([Promise.resolve().then(() => rootFiber.dispose())]);
        process.exit(0);
      })();
    });
    return result;
  });
  ctx.effect(() => {
    transport.start();
    return async () => { await server.shutdown(); transport.close(); };
  }, 'stardew.jsonrpc.serve');
}
