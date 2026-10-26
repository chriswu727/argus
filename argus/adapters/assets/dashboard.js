const byId = id => document.getElementById(id);
const fields = [['state','Session'],['calls','Calls'],['failures','Failures'],['findings','Findings'],['events_dropped','Events evicted']];
async function refresh() {
  try {
    const response = await fetch('/api/status');
    if (!response.ok) throw new Error('Status unavailable');
    const status = await response.json();
    byId('cards').replaceChildren(...fields.map(([key,label]) => {
      const card = document.createElement('div'); card.className='card';
      const title = document.createElement('span'); title.textContent=label;
      const value = document.createElement('strong'); value.textContent=String(status[key]);
      card.append(title,value); return card;
    }));
    byId('operation').textContent=status.operation ? `${status.operation} · ${status.operation_seconds.toFixed(1)} seconds` : 'No active operation';
    byId('progress').textContent=`Last progress: ${new Date(status.last_progress_at*1000).toLocaleTimeString()} · Uptime: ${Math.floor(status.uptime_seconds)} seconds`;
    const testing = status.testing;
    const total = Object.values(testing.goals).reduce((a,b)=>a+b,0);
    byId('testing').textContent=`Phase: ${status.stage || 'idle'} · Goals exercised: ${testing.goals.exercised}/${total} · Blocked: ${testing.goals.blocked} · Checks: ${testing.checks} · Reproduced symptoms: ${testing.verdicts.reproduced} · Inconclusive: ${testing.verdicts.inconclusive} · Observed without recheck: ${testing.verdicts.observed}`;
    byId('goal').textContent=testing.active_goal_indices.length ? `Active goal numbers: ${testing.active_goal_indices.join(', ')}` : 'No goal currently marked in progress';
  } catch { byId('connection').textContent='Status unavailable'; }
}
const events = new EventSource('/api/events');
events.onopen=()=>{byId('connection').textContent='Connected';refresh();};
events.onerror=()=>{byId('connection').textContent='Reconnecting';};
events.addEventListener('gap',()=>{byId('gap').textContent='Some events expired. Current state has been refreshed.';refresh();});
events.addEventListener('runtime',message=>{
  const event=JSON.parse(message.data), item=document.createElement('li');
  item.textContent=`${new Date(event.timestamp*1000).toLocaleTimeString()} · ${event.kind} · ${event.state}${event.operation ? ' · '+event.operation : ''}${event.code ? ' · '+event.code : ''}`;
  byId('events').prepend(item);
  while(byId('events').children.length>60)byId('events').lastChild.remove();
  refresh();
});
refresh();setInterval(refresh,2000);
