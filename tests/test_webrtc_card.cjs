// Exercise the real card lifecycle without a camera or browser credentials.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");

function loadCard() {
  const timers = new Map();
  let serial = 0;
  let Card;
  const document = {};
  const context = {
    HTMLElement: class {}, document, console,
    setTimeout: (fn) => { timers.set(++serial, fn); return serial; },
    clearTimeout: (id) => timers.delete(id),
    clearInterval: () => {},
    customElements: { get: () => undefined, define: (_name, cls) => { Card = cls; } },
  };
  vm.runInNewContext(fs.readFileSync("custom_components/alarmdotcom/www/alarm-webrtc-card.js", "utf8"), context);
  const card = new Card();
  card.isConnected = true;
  return { card, timers, document, context };
}

test("rotation preserves the current stream and resumes the same video", () => {
  const { card, timers } = loadCard();
  let played = 0;
  const video = { srcObject: {}, paused: true, play: () => { played++; return Promise.resolve(); } };
  card.querySelector = () => video;
  card.pc = { close: () => assert.fail("rotation must not close the peer") };
  card.disconnectedCallback();
  assert.equal(timers.size, 1);
  card.connectedCallback();
  assert.equal(timers.size, 0);
  assert.equal(played, 1);
});

test("actual card removal closes transports and invalidates outstanding credentials", () => {
  const { card, timers } = loadCard();
  let closed = 0;
  card.pc = { close: () => { closed++; } };
  card.ws = { close: () => { closed++; } };
  card._secureConfig = { private: "token" };
  card.isConnected = false;
  card.disconnectedCallback();
  [...timers.values()][0]();
  assert.equal(closed, 2);
  assert.equal(card._secureConfig, null);
  assert.equal(card._requestSerial, 1);
});

test("fullscreen uses standard API and falls back to the iPhone player", async () => {
  const { card, document } = loadCard();
  let entered = 0;
  card.querySelector = (name) => name === "ha-card" ? { requestFullscreen: async () => { entered++; } } : {};
  await card._toggleFullscreen();
  assert.equal(entered, 1);
  let exited = 0;
  document.fullscreenElement = {};
  document.exitFullscreen = async () => { exited++; };
  await card._toggleFullscreen();
  assert.equal(exited, 1);
  document.fullscreenElement = null;
  card.querySelector = (name) => name === "video" ? { webkitEnterFullscreen: () => { entered++; } } : {};
  await card._toggleFullscreen();
  assert.equal(entered, 2);
});

test("Janus websocket opening resolves after an open event", async () => {
  const { card, context } = loadCard();
  let socket;
  context.WebSocket = class { constructor() { socket = this; } close() {} };
  const connection = card._connectJanusWebSocket("wss://example.test");
  socket.onopen();
  assert.equal(await connection, socket);
});

test("late Janus connection cannot replace the current stream", async () => {
  const { card, context } = loadCard();
  context.RTCPeerConnection = class {};
  card.querySelector = () => ({});
  let resolveSocket;
  card._openJanusWebSocket = () => new Promise((resolve) => { resolveSocket = resolve; });
  const opening = card._startJanus({});
  card._streamGeneration++;
  let closed = false;
  resolveSocket({ close: () => { closed = true; } });
  await opening;
  assert.equal(closed, true);
  assert.equal(card.ws, null);
});


test("legacy token rejection closes transports and retries with newly requested credentials", async () => {
  const { card, context, timers } = loadCard();
  let closed = 0;
  context.RTCPeerConnection = class { close() { closed++; } };
  let socket;
  context.WebSocket = class { constructor() { socket = this; } close() { closed++; } };
  card.querySelector = () => ({});
  card.renderPlaceholder = () => {};
  card.config = { entity: "camera.test" };
  const oldConfig = { signallingServerUrl: "wss://example.test", signallingServerToken: "old", cameraAuthToken: "old" };
  card._secureConfig = oldConfig;
  card._currentConfig = oldConfig;
  card._connecting = true;
  await card._startLegacy(oldConfig);
  const lateMessage = socket.onmessage;
  await socket.onmessage({ data: JSON.stringify({ error: "Invalid token!" }) });
  assert.equal(closed, 2);
  assert.equal(card._secureConfig, null);
  assert.equal(card._currentConfig, null);
  assert.equal(card._connecting, false);
  assert.equal(timers.size, 1);
  // A queued close/error from the old stream must not consume another retry.
  await lateMessage({ data: JSON.stringify({ error: "Invalid token!" }) });
  assert.equal(card._retryCount, 1);
  let requested = 0;
  card._requestTokens = () => { requested++; };
  [...timers.values()][0]();
  assert.equal(requested, 1);
});

test("identical refreshed configuration can restart a closed stream", () => {
  const { card } = loadCard();
  const config = { signallingServerUrl: "wss://example.test", signallingServerToken: "same", cameraAuthToken: "same" };
  card.config = { entity: "camera.test" };
  card._secureConfig = config;
  card._currentConfig = config;
  card.renderVideo = () => {};
  let started = 0;
  card.startStream = () => { started++; };
  card.hass = { states: { "camera.test": {} } };
  assert.equal(started, 1);
});

test("dashboard return requests credentials and ignores the removed card's pending response", async () => {
  const { card, timers } = loadCard();
  card.config = { entity: "camera.test" };
  card.renderPlaceholder = () => {};
  card.querySelector = () => null;
  let resolveOld;
  let calls = 0;
  const hass = { states: { "camera.test": {} }, connection: { sendMessagePromise: () => {
    calls++;
    return new Promise((resolve) => { resolveOld = resolve; });
  } } };
  card.hass = hass;
  const staleResolve = resolveOld;
  card.isConnected = false;
  card.disconnectedCallback();
  timers.get(card._disconnectGrace)();
  // HA state updates while absent must not start new sessions.
  card.hass = hass;
  assert.equal(calls, 1);
  card.isConnected = true;
  card.connectedCallback();
  assert.equal(calls, 2);
  staleResolve({ signallingServerUrl: "wss://example.test", signallingServerToken: "stale", cameraAuthToken: "stale" });
  await Promise.resolve();
  assert.equal(card._secureConfig, null);
  assert.equal(card._requesting, true);
});

test("retry budget survives state updates and stops repeated token failures", () => {
  const { card, timers } = loadCard();
  card.config = { entity: "camera.test" };
  card.renderPlaceholder = () => {};
  let exhausted = 0;
  card._showReconnectButton = () => { exhausted++; };
  card._retryCount = card._maxRetries;
  card._scheduleRetry("Signaling rejected session");
  card._hass = { states: { "camera.test": {} }, connection: { sendMessagePromise: () => assert.fail("retry budget exhausted") } };
  card.hass = card._hass;
  assert.equal(exhausted, 1);
  assert.equal(timers.size, 0);
});


test("Janus rejection also discards credentials before retrying", async () => {
  const { card, context, timers } = loadCard();
  context.RTCPeerConnection = class { close() {} };
  context.WebSocket = { OPEN: 1 };
  const socket = { readyState: 1, send() {}, close() {} };
  card._openJanusWebSocket = async () => socket;
  card.querySelector = () => ({});
  card.renderPlaceholder = () => {};
  card._secureConfig = { janusToken: "synthetic-old" };
  await card._startJanus(card._secureConfig);
  await socket.onmessage({ data: JSON.stringify({ janus: "error", error: { code: 403, reason: "synthetic" } }) });
  assert.equal(card._secureConfig, null);
  assert.equal(card.ws, null);
  assert.equal(card.pc, null);
  assert.equal(timers.size, 1);
});

test("gateway-supplied error text is rendered as text, never as HTML", () => {
  const { card } = loadCard();
  const nodes = {};
  card.querySelector = (selector) => (nodes[selector] ??= { addEventListener: () => {} });
  const payload = '<img src=x onerror="alert(1)">';

  card._showReconnectButton(`Stream unavailable (Plugin error ${payload})`);
  assert.ok(!card.innerHTML.includes(payload));
  assert.equal(nodes["#reconnect-msg"].textContent, `Stream unavailable (Plugin error ${payload})`);

  card.renderPlaceholder(payload);
  assert.ok(!card.innerHTML.includes(payload));
  assert.equal(nodes["ha-card > div"].textContent, payload);
});

test("Apple SDP patch normalises and adds H.264 profile-level-id per payload type", () => {
  const { card } = loadCard();
  const sdp = [
    "v=0",
    "m=video 9 UDP/TLS/RTP/SAVPF 96 97 98",
    "a=rtpmap:96 H264/90000",
    "a=fmtp:96 packetization-mode=1;profile-level-id=4d4028",
    "a=rtpmap:97 H264/90000",
    "a=fmtp:97 packetization-mode=1",
    "a=rtpmap:98 H264/90000",
    "a=rtpmap:99 VP8/90000",
    "a=fmtp:99 max-fr=30",
    "",
  ].join("\r\n");
  assert.equal(card._patchAppleOfferSdp(sdp), [
    "v=0",
    "m=video 9 UDP/TLS/RTP/SAVPF 96 97 98",
    "a=rtpmap:96 H264/90000",
    "a=fmtp:96 packetization-mode=1;profile-level-id=42e01f",
    "a=rtpmap:97 H264/90000",
    "a=fmtp:97 packetization-mode=1;profile-level-id=42e01f",
    "a=rtpmap:98 H264/90000",
    "a=fmtp:98 profile-level-id=42e01f",
    "a=rtpmap:99 VP8/90000",
    "a=fmtp:99 max-fr=30",
    "",
  ].join("\r\n"));
  assert.equal(card._patchAppleOfferSdp(""), "");
});

test("end-to-end session ICE servers are applied before the offer is answered", async () => {
  const { card, context } = loadCard();
  let pc;
  context.RTCPeerConnection = class {
    constructor(cfg) { pc = this; this.cfg = cfg; }
    getConfiguration() { return this.cfg; }
    setConfiguration(cfg) { this.cfg = cfg; }
    close() {}
  };
  let socket;
  context.WebSocket = class { constructor() { socket = this; } send() {} close() {} };
  card.querySelector = () => ({});
  const config = { signallingServerUrl: "wss://example.test", signallingServerToken: "t", cameraAuthToken: "c", iceServers: [] };
  card._currentConfig = config;
  await card._startLegacy(config);
  const stun = { urls: ["stun:ice.example.test:3478"] };
  const turn = { urls: ["turn:ice.example.test:3478"], username: "u", credential: "p" };
  const turns = { urls: ["turns:ice.example.test:443?transport=tcp"], username: "u", credential: "p" };
  await socket.onmessage({ data: `SESSION_STARTED ${JSON.stringify({ sessionId: "s", iceServers: [stun, turn], tlsTurnServers: [turns] })}` });
  assert.deepEqual(JSON.parse(JSON.stringify(pc.cfg.iceServers)), [stun, turn, turns]);
});

test("malformed session ICE servers are ignored", () => {
  const { card } = loadCard();
  assert.equal(card._sessionIceServers("SESSION_STARTED").length, 0);
  assert.equal(card._sessionIceServers("SESSION_STARTED not-json").length, 0);
  assert.equal(card._sessionIceServers("SESSION_STARTED null").length, 0);
  assert.deepEqual(
    JSON.parse(JSON.stringify(card._sessionIceServers(`SESSION_STARTED ${JSON.stringify({ iceServers: [null, {}, { urls: [] }, { urls: "stun:a" }], tlsTurnServers: "x" })}`))),
    [{ urls: ["stun:a"] }],
  );
});

test("invalid ICE entries are dropped so they cannot void the valid TURN servers", () => {
  const { card } = loadCard();
  const turn = { urls: ["turn:ice.example.test:3478"], username: "u", credential: "p" };
  const servers = card._validIceServers([
    { urls: "" },
    { urls: ["not-a-url", "stun:ice.example.test:3478"] },
    { urls: ["turn:nocreds.example.test:3478"] },
    { urls: ["turns:ice.example.test:443?transport=tcp"], username: "u" },
    { urls: [42, "http://ice.example.test"] },
    turn,
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(servers)), [
    { urls: ["stun:ice.example.test:3478"] },
    turn,
  ]);
});

test("debug log prints states and counts but never URLs or objects", () => {
  const { card, context } = loadCard();
  const lines = [];
  context.console = { debug: (...a) => lines.push(a) };
  card.config = { debug: true };
  card._log("Janus ICE state", "connected");
  card._log("Trying Janus WS URL", "wss://gw.example.test/?token=secret");
  card._log("stats", 30, 2, { secret: "x" });
  assert.deepEqual(JSON.parse(JSON.stringify(lines.map((l) => l.slice(1)))), [
    ["Janus ICE state", "connected"],
    ["Trying Janus WS URL"],
    ["stats", 30, 2],
  ]);
});

test("a transient ICE disconnect keeps the stream; a lasting one rebuilds it", () => {
  const { card, timers } = loadCard();
  let closed = 0, retries = 0;
  const pc = { iceConnectionState: "disconnected", close: () => { closed++; } };
  card.pc = pc;
  card._scheduleRetry = () => { retries++; };
  const gen = card._streamGeneration;
  card._onIceState(pc, gen, "Janus");
  assert.equal(closed, 0);
  pc.iceConnectionState = "connected";
  card._onIceState(pc, gen, "Janus");
  assert.equal(timers.size, 0, "recovery cancels the grace timer");
  pc.iceConnectionState = "disconnected";
  card._onIceState(pc, gen, "Janus");
  [...timers.values()][0]();
  assert.equal(closed, 1);
  assert.equal(retries, 1);
});

test("ICE failure rebuilds the stream immediately", () => {
  const { card } = loadCard();
  let retries = 0;
  const pc = { iceConnectionState: "failed", close() {} };
  card.pc = pc;
  card._scheduleRetry = () => { retries++; };
  card._onIceState(pc, card._streamGeneration, "Legacy");
  assert.equal(retries, 1);
  assert.equal(card.pc, null);
});

test("ICE URLs must follow the stun/turn URI syntax", () => {
  const { card } = loadCard();
  const ok = [
    "stun:ice.example.test", "stun:ice.example.test:3478", "stuns:ice.example.test:5349",
    "turn:10.0.0.1:3478?transport=udp", "turns:ice.example.test:443?transport=tcp", "stun:[2001:db8::1]:3478",
  ];
  const bad = [
    "stun://example.org/foo?x=y", "turn://ice.example.test:3478", "stun:ice.example.test/path",
    "stun:ice.example.test?transport=udp", "turn:ice.example.test?transport=sctp", "stun:ice.example.test:0",
    "stun:ice.example.test:70000", "stun:", "stun:-bad.example", "stun:ice example.test",
  ];
  for (const u of ok) assert.equal(card.constructor._isIceUrl(u), true, u);
  for (const u of bad) assert.equal(card.constructor._isIceUrl(u), false, u);
  const turn = { urls: ["turn:ice.example.test:3478"], username: "u", credential: "p" };
  assert.deepEqual(
    JSON.parse(JSON.stringify(card._validIceServers([{ urls: ["stun://example.org/foo?x=y"] }, turn]))),
    [turn],
  );
});
