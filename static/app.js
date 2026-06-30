// Minimal client: capture mic -> WebSocket (16k PCM); play received TTS (48k PCM).
// All conversation logic (STT, LLM, TTS) lives on the backend.

const micBtn = document.getElementById("micBtn");
const dot = document.getElementById("dot");
const statusText = document.getElementById("statusText");
const conversation = document.getElementById("conversation");

const TTS_RATE = 48000;
let ws, audioCtx, micStream, procNode, playCtx, playHead = 0;
let running = false;

function setStatus(text, state) {
  statusText.textContent = text;
  dot.className = "dot " + state;
}

function addMessage(who, text) {
  const empty = conversation.querySelector(".empty");
  if (empty) empty.remove();
  const el = document.createElement("div");
  el.className = "msg " + who;
  el.innerHTML = `<span class="who">${who === "user" ? "You" : "Assistant"}</span>`;
  el.appendChild(document.createTextNode(text));
  conversation.appendChild(el);
  conversation.scrollTop = conversation.scrollHeight;
}

// Float32 (any rate) -> Int16 PCM at 16 kHz (nearest-sample downsample).
function to16kPCM(input, inRate) {
  const ratio = inRate / 16000;
  const outLen = Math.floor(input.length / ratio);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    let s = input[Math.floor(i * ratio)];
    s = Math.max(-1, Math.min(1, s));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

// Play a chunk of 48 kHz mono Int16 PCM, scheduled back-to-back for smooth audio.
function playPCM(int16) {
  const f = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) f[i] = int16[i] / 32768;
  const buffer = playCtx.createBuffer(1, f.length, TTS_RATE);
  buffer.copyToChannel(f, 0);
  const src = playCtx.createBufferSource();
  src.buffer = buffer;
  src.connect(playCtx.destination);
  const now = playCtx.currentTime;
  if (playHead < now) playHead = now;
  src.start(playHead);
  playHead += buffer.duration;
}

async function start() {
  setStatus("Connecting…", "connecting");
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    setStatus("Microphone access denied", "error");
    return;
  }

  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  playCtx = new (window.AudioContext || window.webkitAudioContext)();
  const inRate = audioCtx.sampleRate;

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/voice`);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    const source = audioCtx.createMediaStreamSource(micStream);
    procNode = audioCtx.createScriptProcessor(4096, 1, 1);
    procNode.onaudioprocess = (e) => {
      if (ws.readyState !== WebSocket.OPEN) return;
      const pcm = to16kPCM(e.inputBuffer.getChannelData(0), inRate);
      ws.send(pcm.buffer);
    };
    source.connect(procNode);
    procNode.connect(audioCtx.destination); // required for the processor to run
  };

  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) {
      playPCM(new Int16Array(ev.data));
      return;
    }
    const m = JSON.parse(ev.data);
    if (m.type === "user") addMessage("user", m.text);
    else if (m.type === "assistant") addMessage("assistant", m.text);
    else if (m.type === "speaking") setStatus(m.value ? "Assistant speaking…" : "Listening…", m.value ? "speaking" : "listening");
    else if (m.type === "ready") setStatus("Listening…", "listening");
    else if (m.type === "status" && m.state === "error") setStatus(m.text, "error");
  };

  ws.onclose = () => { if (running) stop(); };
  ws.onerror = () => setStatus("Connection error", "error");

  running = true;
  micBtn.textContent = "■ Stop";
  micBtn.classList.add("live");
}

function stop() {
  running = false;
  micBtn.textContent = "🎙 Start talking";
  micBtn.classList.remove("live");
  setStatus("Idle — click the mic to start talking", "idle");
  try { if (procNode) procNode.disconnect(); } catch (e) {}
  try { if (micStream) micStream.getTracks().forEach((t) => t.stop()); } catch (e) {}
  try { if (ws && ws.readyState === WebSocket.OPEN) ws.close(); } catch (e) {}
  try { if (audioCtx) audioCtx.close(); } catch (e) {}
  try { if (playCtx) playCtx.close(); } catch (e) {}
}

micBtn.addEventListener("click", () => (running ? stop() : start()));
