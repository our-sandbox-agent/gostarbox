const {Worker} = require('node:worker_threads');
const fs = require('node:fs');
const counters = Array(8).fill(0);
const workers = Array.from({length: 8}, (_, i) => {
  const worker = new Worker('const {parentPort}=require("node:worker_threads"); let n=0; setInterval(()=>parentPort.postMessage(++n),100)', {eval:true});
  worker.on('message', n => {counters[i] = n;});
  return worker;
});
let tick = 0;
setInterval(() => {
  fs.writeFileSync('/workspace/node-heartbeat', String(++tick));
  fs.writeFileSync('/workspace/worker-heartbeats', JSON.stringify(counters));
}, 100);
console.log(JSON.stringify({event:'node_ready', pid:process.pid, workers:workers.length}));
