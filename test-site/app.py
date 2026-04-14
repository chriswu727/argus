"""BuggyTasks — A realistic task management app with 20 intentional bugs.

These bugs are designed to be found through exploratory, user-journey testing —
NOT through simple API testing or scripted E2E. They require actually using
the app like a real person to discover.

=== BUG CATALOG ===

TIER 1 — Surface (any tool finds):
  1. Console ReferenceError on homepage (appConfig undefined)
  2. Dead nav link "Help" → 404
  3. POST /api/newsletter → 500

TIER 2 — Form/Validation (scripted E2E could find):
  4. Login accepts ANY credentials (zero authentication)
  5. Register: mismatched passwords still create account
  6. Register: form data cleared on validation error (UX)
  7. Search XSS: query reflected without escaping

TIER 3 — Logic bugs (only exploratory testing finds):
  8.  Double-submit: clicking "Add Task" fast → duplicate tasks
  9.  Task count off-by-one: dashboard header shows wrong count
  10. Delete fake success: says "Deleted!" but task still exists on refresh
  11. Edit silent failure: "Saved!" toast but data not actually updated
  12. Toggle race condition: rapid complete/incomplete → wrong final state
  13. Pagination duplicates: "Load More" appends same tasks again

TIER 4 — UX/Behavioral (only human-like testing finds):
  14. Empty state says "Loading..." forever instead of "No tasks yet"
  15. Search is case-sensitive: "buy" won't find "Buy groceries"
  16. Date display bug: tasks show "NaN days ago"
  17. Settings shows "Saved!" even when save actually fails (500)
  18. Long titles silently truncated with no tooltip or ellipsis indicator
  19. Priority field accepts negative numbers and absurdly high values
  20. After login, navbar still shows "Login" link instead of user info
  21. Creating a task with only whitespace name succeeds (empty task)
  22. Completing all tasks shows "0 tasks remaining" in red (alarming) instead of success
"""
import json
import time
import html as html_mod
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from starlette.requests import Request

# ── In-memory state ───────────────────────────────────────────────────
_users = {}  # email -> {email, username, password}
_current_user = None  # logged-in user dict or None
_tasks = []  # list of task dicts
_task_id_seq = 0
_settings = {"theme": "dark", "notifications": True, "language": "en"}


def _seed_tasks():
    """Pre-populate some tasks so the app feels real."""
    global _task_id_seq
    seeds = [
        ("Buy groceries", "Milk, eggs, bread, and coffee", "high", False),
        ("Fix login page CSS", "The button overlaps on mobile", "medium", False),
        ("Prepare quarterly report", "Due by end of month", "high", False),
        ("Update dependencies", "npm audit shows 3 vulnerabilities", "low", True),
        ("Schedule team standup", "Move from 9am to 10am", "medium", False),
        ("Review pull request #42", "Alice's auth refactor", "high", False),
        ("Book flight to NYC", "Conference is March 15-17", "medium", True),
        ("Write unit tests for cart", "Coverage is at 47%", "high", False),
    ]
    for title, desc, prio, done in seeds:
        _task_id_seq += 1
        _tasks.append({
            "id": _task_id_seq,
            "title": title,
            "description": desc,
            "priority": prio,
            "done": done,
            "created_at": time.time() - _task_id_seq * 3600 * 24,
        })


_seed_tasks()

# ── Escaping helper ───────────────────────────────────────────────────
esc = html_mod.escape

# ── Layout ────────────────────────────────────────────────────────────
def _nav():
    # BUG #20: Navbar always shows "Login" even when logged in
    # (should show username + logout when _current_user is set)
    return """
    <nav>
      <div class="nav-brand">BuggyTasks</div>
      <div class="nav-links">
        <a href="/">Home</a>
        <a href="/tasks">Tasks</a>
        <a href="/search">Search</a>
        <a href="/settings">Settings</a>
        <a href="/login">Login</a>
        <a href="/register">Register</a>
        <a href="/help">Help</a>
      </div>
    </nav>"""


def _layout(title, body, extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)} — BuggyTasks</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: #f5f5f5; color: #1a1a2e; }}
nav {{ background: #1a1a2e; color: #fff; padding: 12px 32px; display: flex;
       justify-content: space-between; align-items: center; }}
.nav-brand {{ font-weight: 700; font-size: 1.2rem; }}
.nav-links a {{ color: #ccc; text-decoration: none; margin-left: 18px; font-size: .95rem; }}
.nav-links a:hover {{ color: #fff; }}
.container {{ max-width: 800px; margin: 0 auto; padding: 2rem; }}
h1 {{ font-size: 1.8rem; color: #1a1a2e; margin-bottom: .5rem; }}
h2 {{ font-size: 1.3rem; color: #555; margin: 1.5rem 0 .75rem; }}
.card {{ background: #fff; border-radius: 8px; padding: 1.2rem; margin-bottom: 1rem;
         box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 1rem; margin: 1rem 0; }}
.stat {{ text-align: center; }}
.stat-val {{ font-size: 1.8rem; font-weight: 700; color: #1a1a2e; }}
.stat-label {{ font-size: .85rem; color: #888; }}
input, textarea, select {{ width: 100%; padding: 10px; border: 1px solid #ddd;
       border-radius: 6px; font-size: 1rem; margin: 6px 0 12px; }}
button, .btn {{ background: #1a1a2e; color: #fff; border: none; padding: 10px 24px;
       border-radius: 6px; font-size: 1rem; cursor: pointer; text-decoration: none;
       display: inline-block; }}
button:hover, .btn:hover {{ background: #2d2d5e; }}
.btn-danger {{ background: #dc2626; }}
.btn-danger:hover {{ background: #b91c1c; }}
.btn-success {{ background: #16a34a; }}
.btn-sm {{ padding: 6px 14px; font-size: .85rem; }}
.task-item {{ display: flex; align-items: center; gap: 12px; padding: 12px 0;
             border-bottom: 1px solid #eee; }}
.task-item:last-child {{ border-bottom: none; }}
.task-done {{ text-decoration: line-through; color: #999; }}
.task-title {{ flex: 1; font-weight: 500;
              /* BUG #18: titles truncated at 25ch with no ellipsis or tooltip */
              max-width: 25ch; overflow: hidden; white-space: nowrap; }}
.priority {{ font-size: .75rem; padding: 2px 8px; border-radius: 4px; font-weight: 600; }}
.priority-high {{ background: #fee2e2; color: #dc2626; }}
.priority-medium {{ background: #fef3c7; color: #d97706; }}
.priority-low {{ background: #dcfce7; color: #16a34a; }}
.toast {{ position: fixed; top: 20px; right: 20px; padding: 12px 24px; border-radius: 8px;
          color: #fff; font-weight: 500; z-index: 999; display: none; }}
.toast-success {{ background: #16a34a; }}
.toast-error {{ background: #dc2626; }}
.alert {{ padding: 12px 16px; border-radius: 6px; margin: 12px 0; }}
.alert-error {{ background: #fee2e2; color: #dc2626; border: 1px solid #fca5a5; }}
.alert-success {{ background: #dcfce7; color: #16a34a; border: 1px solid #86efac; }}
.alert-warning {{ background: #fef3c7; color: #d97706; border: 1px solid #fcd34d; }}
/* BUG #22: remaining count shown in red even at 0 */
.remaining-zero {{ color: #dc2626; font-weight: 700; }}
.meta {{ color: #999; font-size: .8rem; }}
.search-highlight {{ background: #fef08a; padding: 1px 3px; border-radius: 2px; }}
</style>
{extra_head}
</head>
<body>{_nav()}<div class="container">{body}</div></body></html>"""


def _time_ago(ts):
    """BUG #16: broken time calculation — uses string concat instead of proper math."""
    diff = time.time() - ts
    days = str(diff / 86400)  # This produces "1.5234..." instead of "1"
    # BUG: returns "1.523 days ago" and for very recent items, "0.0002 days ago"
    # which the template turns into NaN in some display paths
    return f"{days[:days.index('.') + 2]} days ago"


# ── Pages ─────────────────────────────────────────────────────────────

async def homepage(request: Request):
    pending = len([t for t in _tasks if not t["done"]])
    done = len([t for t in _tasks if t["done"]])
    body = f"""
    <h1>Welcome to BuggyTasks</h1>
    <p class="meta">A simple task management app. Track your work, stay organized.</p>
    <div class="stats">
      <div class="card stat">
        <div class="stat-val">{pending}</div>
        <div class="stat-label">Pending</div>
      </div>
      <div class="card stat">
        <div class="stat-val">{done}</div>
        <div class="stat-label">Completed</div>
      </div>
      <div class="card stat">
        <!-- BUG #9: Off-by-one — shows total-1 instead of total -->
        <div class="stat-val">{len(_tasks) - 1}</div>
        <div class="stat-label">Total Tasks</div>
      </div>
    </div>
    <div class="card">
      <p>Get started by <a href="/tasks">viewing your tasks</a> or
         <a href="/tasks/new">creating a new one</a>.</p>
    </div>
    """
    # BUG #1: ReferenceError — appConfig is not defined
    extra = '<script>console.log(appConfig.version);</script>'
    return HTMLResponse(_layout("Home", body, extra))


async def login_page(request: Request):
    global _current_user
    if request.method == "POST":
        form = await request.form()
        email = form.get("email", "").strip()
        password = form.get("password", "").strip()
        # BUG #4: Accepts ANY credentials — no validation at all
        _current_user = {"email": email, "username": email.split("@")[0]}
        # BUG #20: After login, we set _current_user but navbar still shows "Login"
        # because _nav() is hardcoded
        body = f"""
        <h1>Login</h1>
        <div class="alert alert-success">Logged in as {esc(email)}</div>
        <a href="/tasks" class="btn">Go to Tasks</a>
        """
        return HTMLResponse(_layout("Login", body))

    body = """
    <h1>Login</h1>
    <div class="card">
      <form method="POST">
        <label>Email</label>
        <input type="email" name="email" placeholder="you@example.com" required>
        <label>Password</label>
        <input type="password" name="password" placeholder="Password" required>
        <button type="submit">Login</button>
      </form>
    </div>
    """
    return HTMLResponse(_layout("Login", body))


async def register_page(request: Request):
    if request.method == "POST":
        form = await request.form()
        username = form.get("username", "").strip()
        email = form.get("email", "").strip()
        password = form.get("password", "")
        confirm = form.get("confirm", "")

        # BUG #21: whitespace-only username passes validation
        if not username or not email:
            # BUG #6: form data lost on error — we show error but don't repopulate fields
            body = """
            <h1>Create Account</h1>
            <div class="alert alert-error">Please fill in all fields.</div>
            """
            return HTMLResponse(_layout("Register", body))

        # BUG #5: Password mismatch check exists but account is created BEFORE the check
        _users[email] = {"email": email, "username": username, "password": password}

        if password != confirm:
            body = """
            <h1>Create Account</h1>
            <div class="alert alert-error">Passwords do not match.</div>
            """
            return HTMLResponse(_layout("Register", body))

        body = f"""
        <h1>Create Account</h1>
        <div class="alert alert-success">Account created for {esc(username)}!</div>
        <a href="/login" class="btn">Go to Login</a>
        """
        return HTMLResponse(_layout("Register", body))

    body = """
    <h1>Create Account</h1>
    <div class="card">
      <form method="POST">
        <label>Username</label>
        <input type="text" name="username" placeholder="Choose a username" required>
        <label>Email</label>
        <input type="email" name="email" placeholder="you@example.com" required>
        <label>Password</label>
        <input type="password" name="password" placeholder="Min 8 characters" required>
        <label>Confirm Password</label>
        <input type="password" name="confirm" placeholder="Confirm password" required>
        <button type="submit">Register</button>
      </form>
    </div>
    """
    return HTMLResponse(_layout("Register", body))


async def tasks_page(request: Request):
    pending = [t for t in _tasks if not t["done"]]
    done_tasks = [t for t in _tasks if t["done"]]
    remaining = len(pending)

    # BUG #22: if remaining is 0, show it in red (alarming) instead of a success state
    remaining_class = ' class="remaining-zero"' if remaining == 0 else ""

    # BUG #14: when no tasks exist, show "Loading..." instead of "No tasks yet"
    if not _tasks:
        task_html = '<div class="card"><p>Loading tasks...</p><div class="spinner"></div></div>'
    else:
        task_html = '<div class="card">'
        for t in _tasks[:5]:  # BUG #13 related: only show first 5
            done_cls = " task-done" if t["done"] else ""
            prio_cls = f"priority-{t['priority']}"
            # BUG #18: title truncated by CSS with no tooltip
            # BUG #16: time_ago shows broken format
            task_html += f"""
            <div class="task-item">
              <input type="checkbox" {"checked" if t["done"] else ""}
                     onclick="toggleTask({t['id']})" id="task-{t['id']}">
              <span class="task-title{done_cls}">{esc(t['title'])}</span>
              <span class="priority {prio_cls}">{t['priority']}</span>
              <span class="meta">{_time_ago(t['created_at'])}</span>
              <button class="btn btn-sm" onclick="editTask({t['id']})">Edit</button>
              <button class="btn btn-sm btn-danger" onclick="deleteTask({t['id']})">Delete</button>
            </div>"""
        task_html += '</div>'

    body = f"""
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <h1>My Tasks</h1>
      <a href="/tasks/new" class="btn">+ New Task</a>
    </div>
    <p{remaining_class}>{remaining} tasks remaining</p>
    {task_html}
    <button class="btn" onclick="loadMore()" style="margin-top:1rem;">Load More</button>

    <div class="toast toast-success" id="toast"></div>
    """

    # BUG #8: No debounce on task creation — double-submit creates duplicates
    # BUG #10: deleteTask shows toast "Deleted!" but doesn't actually remove from server
    # BUG #11: editTask sends update but server ignores the body
    # BUG #12: toggleTask has a race condition — rapid clicks desync state
    js = """
    <script>
    // BUG #1 also fires here
    console.log(appConfig.theme);

    function showToast(msg, type) {
        const t = document.getElementById('toast');
        t.textContent = msg;
        t.className = 'toast toast-' + type;
        t.style.display = 'block';
        setTimeout(() => t.style.display = 'none', 2000);
    }

    async function toggleTask(id) {
        // BUG #12: no locking — rapid clicks send multiple requests
        // Server toggles based on current state, so rapid clicks can desync
        await fetch('/api/tasks/' + id + '/toggle', { method: 'POST' });
        location.reload();
    }

    async function deleteTask(id) {
        // BUG #10: Shows success toast but the API doesn't actually delete
        const resp = await fetch('/api/tasks/' + id, { method: 'DELETE' });
        if (resp.ok) {
            showToast('Task deleted!', 'success');
            // Remove from DOM visually
            const row = document.getElementById('task-' + id);
            if (row) row.closest('.task-item').style.display = 'none';
        }
        // Note: we DON'T reload — so user sees it "deleted" but refresh brings it back
    }

    async function editTask(id) {
        window.location.href = '/tasks/' + id + '/edit';
    }

    // BUG #13: loadMore fetches the same first page of tasks again
    let loadMoreOffset = 0;
    async function loadMore() {
        const resp = await fetch('/api/tasks?offset=' + loadMoreOffset);
        const data = await resp.json();
        // BUG: offset never increments, so same tasks loaded every time
        if (data.tasks.length === 0) {
            showToast('No more tasks', 'error');
            return;
        }
        const container = document.querySelector('.card');
        data.tasks.forEach(t => {
            const div = document.createElement('div');
            div.className = 'task-item';
            div.innerHTML = '<span class="task-title">' + t.title + '</span>' +
                           '<span class="priority priority-' + t.priority + '">' + t.priority + '</span>';
            container.appendChild(div);
        });
        showToast('Loaded ' + data.tasks.length + ' more tasks', 'success');
    }
    </script>"""

    return HTMLResponse(_layout("Tasks", body, js))


async def new_task_page(request: Request):
    if request.method == "POST":
        form = await request.form()
        title = form.get("title", "")
        description = form.get("description", "")
        priority = form.get("priority", "medium")

        # BUG #21: whitespace-only title passes — no strip() or empty check after strip
        # BUG #19: no validation on priority — could be anything the user types
        global _task_id_seq
        _task_id_seq += 1
        _tasks.append({
            "id": _task_id_seq,
            "title": title,  # Not stripped — could be "   "
            "description": description,
            "priority": priority,
            "done": False,
            "created_at": time.time(),
        })

        # BUG #8: No redirect-after-POST — if user refreshes, task is created again
        body = f"""
        <h1>Task Created</h1>
        <div class="alert alert-success">"{esc(title)}" has been added.</div>
        <a href="/tasks" class="btn">Back to Tasks</a>
        <a href="/tasks/new" class="btn">Add Another</a>
        """
        return HTMLResponse(_layout("Task Created", body))

    body = """
    <h1>New Task</h1>
    <div class="card">
      <form method="POST">
        <label>Title</label>
        <input type="text" name="title" placeholder="What needs to be done?" required>
        <label>Description</label>
        <textarea name="description" rows="3" placeholder="Add details..."></textarea>
        <label>Priority</label>
        <!-- BUG #19: This is a text input, not a select — user can type anything -->
        <input type="text" name="priority" placeholder="high, medium, or low" value="medium">
        <button type="submit">Add Task</button>
      </form>
    </div>
    """
    return HTMLResponse(_layout("New Task", body))


async def edit_task_page(request: Request):
    task_id = int(request.path_params["task_id"])
    task = next((t for t in _tasks if t["id"] == task_id), None)
    if not task:
        return HTMLResponse(_layout("Not Found",
            '<h1>Task Not Found</h1><div class="alert alert-error">This task does not exist.</div>'),
            status_code=404)

    if request.method == "POST":
        form = await request.form()
        # BUG #11: We read the form but DON'T actually update the task
        new_title = form.get("title", task["title"])
        new_desc = form.get("description", task["description"])
        new_prio = form.get("priority", task["priority"])

        # Intentionally NOT updating:
        # task["title"] = new_title
        # task["description"] = new_desc
        # task["priority"] = new_prio

        # But we show a success message anyway
        body = f"""
        <h1>Edit Task</h1>
        <div class="alert alert-success">Task "{esc(new_title)}" saved successfully!</div>
        <a href="/tasks" class="btn">Back to Tasks</a>
        """
        return HTMLResponse(_layout("Edit Task", body))

    body = f"""
    <h1>Edit Task</h1>
    <div class="card">
      <form method="POST">
        <label>Title</label>
        <input type="text" name="title" value="{esc(task['title'])}">
        <label>Description</label>
        <textarea name="description" rows="3">{esc(task['description'])}</textarea>
        <label>Priority</label>
        <input type="text" name="priority" value="{esc(task['priority'])}">
        <button type="submit">Save Changes</button>
      </form>
    </div>
    """
    return HTMLResponse(_layout("Edit Task", body))


async def search_page(request: Request):
    query = request.query_params.get("q", "")
    results_html = ""
    if query:
        # BUG #15: Case-sensitive search — "buy" won't match "Buy groceries"
        results = [t for t in _tasks if query in t["title"] or query in t["description"]]

        if results:
            results_html = '<div class="card">'
            for t in results:
                prio_cls = f"priority-{t['priority']}"
                results_html += f"""
                <div class="task-item">
                  <span class="task-title">{esc(t['title'])}</span>
                  <span class="priority {prio_cls}">{t['priority']}</span>
                </div>"""
            results_html += '</div>'
        else:
            results_html = '<div class="card"><p>No results found.</p></div>'

        # BUG #7: XSS — query reflected without escaping in the heading
        results_html = f'<h2>Results for: {query}</h2>' + results_html

    body = f"""
    <h1>Search Tasks</h1>
    <div class="card">
      <form method="GET">
        <input type="text" name="q" placeholder="Search tasks..." value="{esc(query)}">
        <button type="submit">Search</button>
      </form>
    </div>
    {results_html}
    """
    return HTMLResponse(_layout("Search", body))


async def settings_page(request: Request):
    if request.method == "POST":
        # BUG #17: Server returns 500 on settings save, but JS shows "Saved!" anyway
        raise ValueError("Settings DB connection failed")

    body = """
    <h1>Settings</h1>
    <div class="card">
      <form id="settings-form">
        <label>Display Name</label>
        <input type="text" name="display_name" value="User">
        <label>Theme</label>
        <select name="theme">
          <option value="dark">Dark</option>
          <option value="light">Light</option>
        </select>
        <label><input type="checkbox" name="notifications" checked> Email notifications</label>
        <br><br>
        <button type="button" onclick="saveSettings()">Save Settings</button>
      </form>
    </div>
    <div class="toast toast-success" id="toast"></div>
    """

    js = """
    <script>
    async function saveSettings() {
        const form = document.getElementById('settings-form');
        const data = new FormData(form);
        try {
            const resp = await fetch('/settings', { method: 'POST', body: data });
            // BUG #17: Shows success regardless of response status
            document.getElementById('toast').textContent = 'Settings saved!';
            document.getElementById('toast').className = 'toast toast-success';
            document.getElementById('toast').style.display = 'block';
            setTimeout(() => document.getElementById('toast').style.display = 'none', 2000);
        } catch(e) {
            // Even network errors show success due to the above running first
            // Actually the success toast is inside try, so real network errors would go here
            // But 500 responses don't throw — they still trigger the "Saved!" toast
        }
    }
    </script>"""

    return HTMLResponse(_layout("Settings", body, js))


async def help_page(request: Request):
    # BUG #2: This page doesn't exist — returns 404
    return HTMLResponse(_layout("404 — Not Found",
        '<h1>404 — Page Not Found</h1>'
        '<div class="alert alert-error">The page you\'re looking for doesn\'t exist.</div>'
        '<a href="/" class="btn">Go Home</a>'),
        status_code=404)


async def not_found(request: Request, exc):
    return HTMLResponse(_layout("404",
        '<h1>404 — Page Not Found</h1>'
        '<div class="alert alert-error">The page you\'re looking for doesn\'t exist.</div>'
        '<a href="/" class="btn">Go Home</a>'),
        status_code=404)


async def server_error(request: Request, exc):
    return HTMLResponse(
        '<h1>500 Internal Server Error</h1><p>Something went wrong.</p>',
        status_code=500)


# ── API Endpoints ─────────────────────────────────────────────────────

async def api_tasks(request: Request):
    """GET /api/tasks — list tasks (paginated). BUG #13: offset is ignored."""
    # BUG #13: We receive offset param but always return the first 5 tasks
    offset = int(request.query_params.get("offset", 0))
    # Intentionally ignoring offset:
    page = _tasks[:5]
    return JSONResponse({"tasks": page, "total": len(_tasks)})


async def api_create_task(request: Request):
    """POST /api/tasks — create a task."""
    global _task_id_seq
    data = await request.json()
    _task_id_seq += 1
    task = {
        "id": _task_id_seq,
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "priority": data.get("priority", "medium"),
        "done": False,
        "created_at": time.time(),
    }
    _tasks.append(task)
    return JSONResponse(task, status_code=201)


async def api_delete_task(request: Request):
    """DELETE /api/tasks/{id} — BUG #10: responds 200 but doesn't actually delete."""
    task_id = int(request.path_params["task_id"])
    task = next((t for t in _tasks if t["id"] == task_id), None)
    if not task:
        return JSONResponse({"error": "not found"}, status_code=404)
    # BUG #10: we DON'T remove the task from _tasks
    # _tasks.remove(task)  ← this line is intentionally commented out
    return JSONResponse({"status": "deleted", "id": task_id})


async def api_toggle_task(request: Request):
    """POST /api/tasks/{id}/toggle — toggle done status."""
    task_id = int(request.path_params["task_id"])
    task = next((t for t in _tasks if t["id"] == task_id), None)
    if not task:
        return JSONResponse({"error": "not found"}, status_code=404)
    # BUG #12: Toggle works, but no protection against race conditions.
    # If two rapid requests come in, they both read the same state and
    # both flip it, resulting in no net change.
    task["done"] = not task["done"]
    return JSONResponse({"id": task_id, "done": task["done"]})


async def api_update_task(request: Request):
    """PUT /api/tasks/{id} — BUG #11: accepts request but doesn't update."""
    task_id = int(request.path_params["task_id"])
    task = next((t for t in _tasks if t["id"] == task_id), None)
    if not task:
        return JSONResponse({"error": "not found"}, status_code=404)
    # BUG #11: we read the body but don't apply changes
    data = await request.json()
    # Intentionally NOT updating task with data
    return JSONResponse({"status": "updated", "task": task})


async def api_newsletter(request: Request):
    """POST /api/newsletter — BUG #3: always crashes."""
    raise RuntimeError("Newsletter service unavailable")


# ── App ───────────────────────────────────────────────────────────────

routes = [
    Route("/", homepage),
    Route("/login", login_page, methods=["GET", "POST"]),
    Route("/register", register_page, methods=["GET", "POST"]),
    Route("/tasks", tasks_page),
    Route("/tasks/new", new_task_page, methods=["GET", "POST"]),
    Route("/tasks/{task_id:int}/edit", edit_task_page, methods=["GET", "POST"]),
    Route("/search", search_page),
    Route("/settings", settings_page, methods=["GET", "POST"]),
    Route("/help", help_page),
    # API
    Route("/api/tasks", api_tasks, methods=["GET"]),
    Route("/api/tasks", api_create_task, methods=["POST"]),
    Route("/api/tasks/{task_id:int}", api_delete_task, methods=["DELETE"]),
    Route("/api/tasks/{task_id:int}", api_update_task, methods=["PUT"]),
    Route("/api/tasks/{task_id:int}/toggle", api_toggle_task, methods=["POST"]),
    Route("/api/newsletter", api_newsletter, methods=["POST"]),
]

app = Starlette(
    routes=routes,
    exception_handlers={404: not_found, 500: server_error},
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5555)
