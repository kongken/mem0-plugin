/**
 * mem0-oss 的 pi 扩展。
 *
 * 只做事件翻译:把 pi 的生命周期事件变成 JSON,喂给同仓的 `bin/mem0-oss pi-hook <事件>`,
 * 召回、脱敏、暂存、配对、捕获全部复用 Python 核心,与 Claude Code / Codex / Cursor
 * 语义一致(见 lib/mem0_oss/pi_hooks.py)。
 *
 *   session_start       认证探活;服务端拒绝认证时 notify 提醒一次
 *   before_agent_start  召回,结果作为隐藏的自定义消息注入本轮;同时暂存脱敏后的 prompt
 *   context             只保留最新一条召回消息,旧的不随会话历史累积
 *   agent_end           取最后一条 assistant 文本,与暂存的 prompt 配对后异步写入
 *
 * 全程 fail-open:Python 缺失、服务端挂了、超时,pi 会话照常可用,只是没有记忆。
 * MEM0_BASE_URL 未设置时整个扩展不做任何事(与其他宿主一致,它是总开关)。
 */

import { spawn, type ChildProcessByStdio } from "node:child_process";
import type { Readable, Writable } from "node:stream";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export const RECALL_MESSAGE_TYPE = "mem0-recall";

/** 外层兜底超时。Python 侧各自有更短的请求超时,这里只防进程挂死。 */
const HOOK_TIMEOUT_MS = 15_000;

type HookEvent = "session-start" | "user-prompt" | "agent-end";

interface HookPayload {
	session_id: string;
	cwd: string;
	prompt?: string;
	text?: string;
}

export interface Runner {
	(event: HookEvent, payload: HookPayload): Promise<string | undefined>;
}

function defaultBin(): string {
	const here = dirname(fileURLToPath(import.meta.url));
	return resolve(here, "..", "..", "bin", "mem0-oss");
}

function defaultPython(): string {
	return process.platform === "win32" ? "python" : "python3";
}

/**
 * 起一次 `mem0-oss pi-hook <event>`,返回它输出的 `text`(没有则 undefined)。永不 reject。
 */
export function createRunner(env: NodeJS.ProcessEnv = process.env): Runner {
	const bin = env.MEM0_OSS_BIN || defaultBin();
	const python = env.MEM0_OSS_PYTHON || defaultPython();

	return (event, payload) =>
		new Promise((resolvePromise) => {
			let settled = false;
			const finish = (value: string | undefined) => {
				if (settled) return;
				settled = true;
				clearTimeout(timer);
				resolvePromise(value);
			};

			let child: ChildProcessByStdio<Writable, Readable, null>;
			try {
				child = spawn(python, [bin, "pi-hook", event], {
					cwd: payload.cwd || undefined,
					env,
					stdio: ["pipe", "pipe", "ignore"],
				});
			} catch {
				resolvePromise(undefined);
				return;
			}
			const timer = setTimeout(() => {
				child.kill("SIGKILL");
				finish(undefined);
			}, HOOK_TIMEOUT_MS);

			let stdout = "";
			child.stdout.setEncoding("utf8");
			child.stdout.on("data", (chunk: string) => {
				stdout += chunk;
			});
			child.on("error", () => finish(undefined));
			child.on("close", () => finish(parseText(stdout)));
			// 子进程提前退出时 stdin 会 EPIPE,吞掉即可
			child.stdin.on("error", () => {});
			child.stdin.end(JSON.stringify(payload));
		});
}

function parseText(stdout: string): string | undefined {
	const line = stdout.trim();
	if (!line) return undefined;
	try {
		const parsed = JSON.parse(line);
		return typeof parsed?.text === "string" && parsed.text ? parsed.text : undefined;
	} catch {
		return undefined;
	}
}

/** 取 agent_end 消息列表里最后一条正常结束的 assistant 文本。 */
export function lastAssistantText(messages: readonly unknown[] | undefined): string {
	if (!Array.isArray(messages)) return "";
	for (let i = messages.length - 1; i >= 0; i--) {
		const message = messages[i] as {
			role?: string;
			stopReason?: string;
			content?: unknown;
		};
		if (message?.role !== "assistant") continue;
		// 被中断或出错的一轮不是一次完整的回答,不写进记忆
		if (message.stopReason === "aborted" || message.stopReason === "error") return "";
		if (typeof message.content === "string") return message.content.trim();
		if (!Array.isArray(message.content)) return "";
		return message.content
			.filter((block: { type?: string; text?: unknown }) => block?.type === "text")
			.map((block: { text?: unknown }) => (typeof block.text === "string" ? block.text : ""))
			.join("\n")
			.trim();
	}
	return "";
}

/** 召回消息只留最新一条:每次输入都会注入一块,不裁剪就会随会话历史越积越多。 */
export function keepLatestRecall<T>(messages: readonly T[]): T[] | undefined {
	const isRecall = (m: unknown) =>
		(m as { role?: string; customType?: string })?.role === "custom" &&
		(m as { customType?: string }).customType === RECALL_MESSAGE_TYPE;
	let last = -1;
	messages.forEach((m, i) => {
		if (isRecall(m)) last = i;
	});
	const kept = messages.filter((m, i) => !isRecall(m) || i === last);
	return kept.length === messages.length ? undefined : kept;
}

export function createExtension(runner: Runner, env: NodeJS.ProcessEnv = process.env) {
	return function (pi: ExtensionAPI): void {
		if (!env.MEM0_BASE_URL) return;

		const sessionId = (ctx: { sessionManager?: { getSessionId?: () => string } }): string => {
			try {
				return ctx.sessionManager?.getSessionId?.() ?? "";
			} catch {
				return "";
			}
		};

		pi.on("session_start", async (event, ctx) => {
			if (event.reason === "reload") return;
			const warning = await runner("session-start", {
				session_id: sessionId(ctx),
				cwd: ctx.cwd,
			});
			if (warning && ctx.hasUI) ctx.ui.notify(warning, "warning");
		});

		pi.on("before_agent_start", async (event, ctx) => {
			// 由 pi.sendMessage(..., { triggerTurn: true }) 起的 run 没有用户输入
			if ((event as { messageType?: string }).messageType === "custom") return;
			const prompt = (event as { prompt?: string }).prompt;
			if (typeof prompt !== "string" || !prompt.trim()) return;

			const recalled = await runner("user-prompt", {
				session_id: sessionId(ctx),
				cwd: ctx.cwd,
				prompt,
			});
			if (!recalled) return;
			return {
				message: {
					customType: RECALL_MESSAGE_TYPE,
					content: recalled,
					display: false,
				},
			};
		});

		pi.on("context", async (event) => {
			const kept = keepLatestRecall(event.messages);
			return kept ? { messages: kept } : undefined;
		});

		pi.on("agent_end", async (event, ctx) => {
			const text = lastAssistantText(event.messages);
			if (!text) return;
			await runner("agent-end", {
				session_id: sessionId(ctx),
				cwd: ctx.cwd,
				text,
			});
		});
	};
}

export default createExtension(createRunner());
