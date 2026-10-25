// Cooperative cancellation for the CLI owned by this game invocation.
import { existsSync } from 'node:fs';
const path = process.env.STARDEW_MCODE_CANCEL_FILE;
if (path) {
  const timer = setInterval(() => {
    if (existsSync(path) && process.listenerCount('SIGINT') > 0) {
      clearInterval(timer);
      process.emit('SIGINT');
    }
  }, 100);
  timer.unref();
}
