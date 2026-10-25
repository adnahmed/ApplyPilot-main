import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  createConcurrently,
  Logger,
  LogError,
  LogExit,
  LogOutput,
} from "concurrently";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const python = path.join(
  root,
  ".venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
const runtime = path.join(root, "scripts/local_runtime.py");
const commands = {
  dashboard: ["dashboard", "--no-open"],
  activities: [
    "temporal",
    "worker",
    "--roles",
    "preparation,discovery,research,models,documents,learning,network,email,social,reporting,maintenance,egress,browser",
  ],
  applications: ["temporal", "worker", "--roles", "applications"],
};
const logger = new Logger({ prefixFormat: "[{name}]" });
const children = new Set();
let interrupted = false;
let stopping = false;
const stopChildren = () => {
  if (stopping) return;
  stopping = true;
  for (const child of children) child.stdin?.end();
};
const onInterrupt = () => {
  interrupted = true;
  stopChildren();
};

// Terminal Ctrl+C reaches the attached Python processes. Let their native
// shutdown finish; concurrently's default Windows kill uses taskkill /F.
process.on("SIGINT", onInterrupt);
process.on("SIGTERM", onInterrupt);
try {
  const { result } = createConcurrently(
    Object.keys(commands).map((name) => ({ name, command: name })),
    {
      cwd: root,
      logger,
      outputStream: process.stdout,
      prefixColors: ["cyan", "green", "magenta"],
      controllers: [
        new LogError({ logger }),
        new LogOutput({ logger }),
        new LogExit({ logger }),
      ],
      spawn(command, options) {
        const child = spawn(python, ["-u", runtime, ...commands[command]], {
          ...options,
          env: { ...options.env, APPLYPILOT_LOCAL_ROLE: command },
          stdio: ["pipe", "pipe", "pipe"],
          detached: false,
          windowsHide: false,
        });
        children.add(child);
        child.stdin.on("error", () => {});
        child.once("close", () => {
          children.delete(child);
          stopChildren();
        });
        child.once("error", stopChildren);
        if (stopping) child.stdin.end();
        return child;
      },
    },
  );
  await result;
} catch (error) {
  if (error instanceof Error) console.error(error);
  process.exitCode = interrupted ? 0 : 1;
} finally {
  stopChildren();
  process.off("SIGINT", onInterrupt);
  process.off("SIGTERM", onInterrupt);
}
