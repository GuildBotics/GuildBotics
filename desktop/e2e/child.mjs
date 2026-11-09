import { spawn } from "node:child_process";

// A child process together with the promise of its end, made when it starts.
// It settles with why the child ended -- an exit code, a signal, or failing to
// start, after which no exit comes -- even for one that already has, so whoever
// watches or stops the child later waits on it rather than on an event.
export function start(command, args, options) {
  const child = spawn(command, args, options);
  const ended = new Promise((resolve) => {
    child.once("exit", (code, signal) => resolve(signal ?? `exit code ${code}`));
    child.once("error", (error) => resolve(error.message));
  });
  return { child, ended };
}

// Ask the process to stop and settle once it has ended.
export async function stop({ child, ended }) {
  child.kill("SIGTERM");
  await ended;
}
