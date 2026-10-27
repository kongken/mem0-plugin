// 零依赖:用 node 自带的 test runner 直接跑 .ts(Node >= 22.18 / 24)。
// 运行:node --test pi/tests/

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, it } from "node:test";

import {
	RECALL_MESSAGE_TYPE,
	createExtension,
	createRunner,
	keepLatestRecall,
	lastAssistantText,
	type Runner,
} from "../extensions/mem0-oss.ts";

const LONG_PROMPT = "一条足够长的用户输入,确保会触发召回检索";

type Handler = (event: any, ctx: any) => any;

function fakePi() {
	const handlers = new Map<string, Handler>();
	return {
		pi: { on: (name: string, handler: Handler) => void handlers.set(name, handler) } as any,
		fire: (name: string, event: any, ctx: any) => handlers.get(name)?.(event, ctx),
		has: (name: string) => handlers.has(name),
	};
}

function makeCtx() {
	const notices: Array<[string, string | undefined]> = [];
	return {
		notices,
		ctx: {
			cwd: "/tmp/proj",
			hasUI: true,
			sessionManager: { getSessionId: () => "sess-1" },
			ui: { notify: (m: string, t?: string) => void notices.push([m, t]) },
		},
	};
}

describe("lastAssistantText", () => {
	it("joins text blocks of the last assistant message and ignores thinking/tool calls", () => {
		const got = lastAssistantText([
			{ role: "user", content: "hi" },
			{ role: "assistant", content: [{ type: "text", text: "旧的" }] },
			{
				role: "assistant",
				stopReason: "stop",
				content: [
					{ type: "thinking", thinking: "secret" },
					{ type: "text", text: "第一段" },
					{ type: "toolCall", name: "x" },
					{ type: "text", text: "第二段" },
				],
			},
		]);
		assert.equal(got, "第一段\n第二段");
	});

	it("skips trailing non-assistant messages", () => {
		const got = lastAssistantText([
			{ role: "assistant", content: [{ type: "text", text: "答" }] },
			{ role: "toolResult", content: [{ type: "text", text: "工具输出" }] },
		]);
		assert.equal(got, "答");
	});

	it("returns empty for aborted or errored runs", () => {
		for (const stopReason of ["aborted", "error"]) {
			assert.equal(
				lastAssistantText([{ role: "assistant", stopReason, content: [{ type: "text", text: "半截" }] }]),
				"",
			);
		}
	});

	it("returns empty for no assistant message or bad input", () => {
		assert.equal(lastAssistantText([{ role: "user", content: "x" }]), "");
		assert.equal(lastAssistantText(undefined), "");
	});
});

describe("keepLatestRecall", () => {
	const recall = (n: number) => ({ role: "custom", customType: RECALL_MESSAGE_TYPE, content: `r${n}` });

	it("keeps only the newest recall message", () => {
		const user = { role: "user", content: "u" };
		const got = keepLatestRecall([recall(1), user, recall(2), user, recall(3)]);
		assert.deepEqual(got, [user, user, recall(3)]);
	});

	it("leaves other custom messages alone and returns undefined when nothing changes", () => {
		const other = { role: "custom", customType: "something-else", content: "x" };
		assert.equal(keepLatestRecall([other, recall(1)]), undefined);
		assert.deepEqual(keepLatestRecall([other, recall(1), recall(2)]), [other, recall(2)]);
	});
});

describe("extension wiring", () => {
	const calls: Array<[string, any]> = [];
	const runnerReturning =
		(byEvent: Record<string, string | undefined>): Runner =>
		async (event, payload) => {
			calls.push([event, payload]);
			return byEvent[event];
		};

	it("registers nothing when MEM0_BASE_URL is unset", () => {
		const { pi, has } = fakePi();
		createExtension(runnerReturning({}), {})(pi);
		assert.equal(has("before_agent_start"), false);
	});

	it("injects recall as a hidden custom message", async () => {
		calls.length = 0;
		const { pi, fire } = fakePi();
		createExtension(runnerReturning({ "user-prompt": "## Recalled" }), { MEM0_BASE_URL: "http://x" })(pi);
		const { ctx } = makeCtx();
		const got = await fire("before_agent_start", { prompt: LONG_PROMPT }, ctx);
		assert.deepEqual(got, {
			message: { customType: RECALL_MESSAGE_TYPE, content: "## Recalled", display: false },
		});
		assert.deepEqual(calls[0], [
			"user-prompt",
			{ session_id: "sess-1", cwd: "/tmp/proj", prompt: LONG_PROMPT },
		]);
	});

	it("injects nothing when recall is empty, the prompt is blank, or the run is custom", async () => {
		calls.length = 0;
		const { pi, fire } = fakePi();
		createExtension(runnerReturning({}), { MEM0_BASE_URL: "http://x" })(pi);
		const { ctx } = makeCtx();
		assert.equal(await fire("before_agent_start", { prompt: LONG_PROMPT }, ctx), undefined);
		assert.equal(await fire("before_agent_start", { prompt: "  " }, ctx), undefined);
		assert.equal(await fire("before_agent_start", { messageType: "custom", message: {} }, ctx), undefined);
		assert.equal(calls.length, 1, "blank / custom runs must not reach the runner");
	});

	it("notifies on session_start warnings but not on reload", async () => {
		const { pi, fire } = fakePi();
		createExtension(runnerReturning({ "session-start": "认证被拒" }), { MEM0_BASE_URL: "http://x" })(pi);
		const { ctx, notices } = makeCtx();
		await fire("session_start", { reason: "reload" }, ctx);
		assert.deepEqual(notices, []);
		await fire("session_start", { reason: "startup" }, ctx);
		assert.deepEqual(notices, [["认证被拒", "warning"]]);
	});

	it("captures the last assistant text on agent_end and skips empty runs", async () => {
		calls.length = 0;
		const { pi, fire } = fakePi();
		createExtension(runnerReturning({}), { MEM0_BASE_URL: "http://x" })(pi);
		const { ctx } = makeCtx();
		await fire("agent_end", { messages: [{ role: "assistant", content: [{ type: "text", text: "答" }] }] }, ctx);
		await fire("agent_end", { messages: [{ role: "user", content: "x" }] }, ctx);
		assert.deepEqual(calls, [["agent-end", { session_id: "sess-1", cwd: "/tmp/proj", text: "答" }]]);
	});

	it("prunes old recall messages in the context event", async () => {
		const { pi, fire } = fakePi();
		createExtension(runnerReturning({}), { MEM0_BASE_URL: "http://x" })(pi);
		const r = (c: string) => ({ role: "custom", customType: RECALL_MESSAGE_TYPE, content: c });
		const got = await fire("context", { messages: [r("old"), { role: "user", content: "u" }, r("new")] }, {});
		assert.deepEqual(got.messages.map((m: any) => m.content), ["u", "new"]);
		assert.equal(await fire("context", { messages: [r("only")] }, {}), undefined);
	});
});

// 真跑一遍:扩展 → 起 python3 bin/mem0-oss pi-hook → 打到假的 mem0 server
describe("end to end against a fake mem0 server", () => {
	const requests: Array<{ method: string; path: string; body: any }> = [];
	let server: http.Server;
	let stateDir: string;
	let env: NodeJS.ProcessEnv;

	before(async () => {
		stateDir = mkdtempSync(join(tmpdir(), "mem0-pi-"));
		server = http.createServer((req, res) => {
			let raw = "";
			req.on("data", (chunk) => (raw += chunk));
			req.on("end", () => {
				requests.push({ method: req.method!, path: req.url!, body: raw ? JSON.parse(raw) : undefined });
				res.setHeader("Content-Type", "application/json");
				res.end(
					JSON.stringify(
						req.url === "/search" ? { results: [{ id: "m1", memory: "用户偏好 vim" }] } : { results: [] },
					),
				);
			});
		});
		await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
		const { port } = server.address() as { port: number };
		env = {
			...process.env,
			MEM0_BASE_URL: `http://127.0.0.1:${port}`,
			MEM0_API_KEY: "m0sk_test",
			MEM0_USER_ID: "alice",
			MEM0_USER_ID_SCOPE: "exact",
			MEM0_STATE_DIR: stateDir,
		};
	});

	after(async () => {
		await new Promise((done) => server.close(done));
		rmSync(stateDir, { recursive: true, force: true });
	});

	it("recalls on before_agent_start and captures the pair on agent_end", async () => {
		const { pi, fire } = fakePi();
		createExtension(createRunner(env), env)(pi);
		const { ctx } = makeCtx();
		ctx.cwd = stateDir;

		const injected = await fire("before_agent_start", { prompt: LONG_PROMPT }, ctx);
		assert.match(injected.message.content, /Recalled Memories/);
		assert.match(injected.message.content, /用户偏好 vim/);
		assert.equal(injected.message.display, false);

		await fire(
			"agent_end",
			{ messages: [{ role: "assistant", stopReason: "stop", content: [{ type: "text", text: "好的" }] }] },
			ctx,
		);

		// capture 是脱离进程组的子进程,异步落到服务端
		const deadline = Date.now() + 10_000;
		while (Date.now() < deadline && !requests.some((r) => r.method === "POST" && r.path === "/memories")) {
			await new Promise((done) => setTimeout(done, 50));
		}
		const capture = requests.find((r) => r.method === "POST" && r.path === "/memories");
		assert.ok(capture, "capture did not reach the server");
		assert.equal(capture.body.metadata.source, "pi");
		assert.equal(capture.body.user_id, "alice");
		assert.deepEqual(
			capture.body.messages.map((m: any) => [m.role, m.content]),
			[
				["user", LONG_PROMPT],
				["assistant", "好的"],
			],
		);
	});

	it("fails open when python cannot be started", async () => {
		const runner = createRunner({ ...env, MEM0_OSS_PYTHON: "definitely-not-a-python" });
		assert.equal(await runner("user-prompt", { session_id: "s", cwd: stateDir, prompt: LONG_PROMPT }), undefined);
	});
});
