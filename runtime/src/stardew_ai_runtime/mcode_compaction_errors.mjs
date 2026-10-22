// Instrument only the context-maintenance child process. Persist status/code
// metadata, never credentials, headers, prompts, model output, or response bodies.
import { appendFileSync } from 'node:fs';
const output = process.env.STARDEW_MCODE_COMPACTION_ERROR_METADATA;
const original = globalThis.fetch;
if (output && typeof original === 'function') {
  globalThis.fetch = async function (...args) {
    const response = await original.apply(this, args);
    try {
      if (response.headers.get('content-type')?.includes('application/json')) {
        const body = await response.clone().json();
        const message = body?.base_resp?.status_msg ?? body?.error?.message;
        const rawCode = body?.base_resp?.status_code ?? body?.error?.code;
        const suffixCode = typeof message === 'string' ? /\((\d{4,6})\)/.exec(message)?.[1] : undefined;
        const code = typeof rawCode === 'number' || typeof rawCode === 'string' ? rawCode : suffixCode;
        if (!response.ok || (code !== undefined && String(code) !== '0')) {
          appendFileSync(output, JSON.stringify({
            atUtc: new Date().toISOString(), httpStatus: response.status, code: code ?? null,
          }) + '\n', 'utf8');
        }
      }
    } catch { /* Metadata collection never changes the provider response. */ }
    return response;
  };
}
