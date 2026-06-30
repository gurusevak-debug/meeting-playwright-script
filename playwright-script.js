let pc = null, ws = null, localStream = null;
const sessionRecs = [];

// ── Start Connection & Streaming ───────────────────────────────────────────
async function startSession() {
  try {
    // 1. Acquire audio stream from microphone
    localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  } catch (e) {
    console.error('Microphone access denied:', e.message);
    return;
  }

  // 2. Open signaling WebSocket
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://unscrew-oblong-joyride.ngrok-free.dev/ws/signal`);

  ws.onopen = async () => {
    // 3. Initialize PeerConnection
    pc = new RTCPeerConnection({
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
    });

    // 4. Attach microphone tracks to the WebRTC connection
    localStream.getTracks().forEach(t => pc.addTrack(t, localStream));

    // 5. Handle local ICE candidates and send them to the server
    pc.onicecandidate = e => {
      if (e.candidate && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'candidate', candidate: e.candidate }));
      }
    };

    pc.onconnectionstatechange = () => {
      console.log('PeerConnection state:', pc.connectionState);
    };

    // 6. Create and send SDP Offer
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    ws.send(JSON.stringify({ type: 'offer', sdp: offer.sdp }));
  };

  // 7. Handle incoming signaling and server events
  ws.onmessage = async ({ data }) => {
    const msg = JSON.parse(data);

    if (msg.type === 'answer') {
      await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: msg.sdp }));

    } else if (msg.type === 'candidate') {
      try {
        await pc.addIceCandidate(msg.candidate);
      } catch (e) { 
        console.error('Error adding ICE candidate:', e); 
      }

    } else if (msg.type === 'recording_started') {
      sessionRecs.push(msg.filename);
      console.log('Server started recording:', msg.filename);

    } else if (msg.type === 'recording_stopped') {
      console.log('Server saved recording:', msg.filename);
      
    } else if (msg.type === 'error') {
      console.error('Server error:', msg.message);
    }
  };

  ws.onclose = () => console.log('WebSocket closed');
  ws.onerror = e => console.error('WebSocket error', e);
}

// ── Stop Connection & Streaming ────────────────────────────────────────────
function stopSession() {
  // 1. Tell the server to stop recording if WebSocket is still open
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'stop' }));
  }

  // 2. Stop microphone tracks to turn off recording indicator light
  if (localStream) {
    localStream.getTracks().forEach(t => t.stop());
  }

  // 3. Tear down the PeerConnection
  if (pc) {
    pc.close();
    pc = null;
  }
}

startSession()