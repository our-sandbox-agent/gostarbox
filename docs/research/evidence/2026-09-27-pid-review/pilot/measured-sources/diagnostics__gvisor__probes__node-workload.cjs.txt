const {Worker} = require('node:worker_threads');
const fs = require('node:fs');
const workers = Array.from({length: 8}, () => new Worker('setInterval(() => Math.sqrt(123456), 50)', {eval:true}));
let tick = 0;
setInterval(() => fs.writeFileSync('/workspace/node-heartbeat', String(++tick)), 100);
console.log(JSON.stringify({event:'node_ready', pid:process.pid, workers:workers.length}));
