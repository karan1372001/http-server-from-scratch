# HTTP Server From Scratch

I built this because I wanted to actually understand what happens
underneath a framework like FastAPI, instead of just using one. So I
built an entire HTTP server myself — from raw TCP sockets, parsing real
HTTP requests by hand, building responses byte by byte. No FastAPI, no
Flask, no framework at all.

I'm doing my Master's in Computer Science, and I already have another
project (`ai-agent-assistant`) that uses FastAPI. That one shows I can
use a professional framework. This one shows I understand what that
framework is actually doing under the hood.

I built this over 6 phases, testing and actually running each one for
real before moving to the next one.

## What's in here

1. **Basic HTTP server** — raw sockets, parsing real HTTP requests by
   hand, sending back real responses, keeping connections alive
2. **Routing and files** — URLs with parameters like `/users/1`, reading
   uploaded files, serving files safely (blocking path traversal attacks)
3. **Handling many users at once** — built two different ways to do this
   (a thread pool, and an async event loop) and benchmarked them
4. **Making it production-ready** — HTTPS, compression, caching, and 2
   real security fixes
5. **Extra features** — WebSockets (live chat-style connections), rate
   limiting, and a reverse proxy
6. **Proving it actually works** — benchmarked it against real servers
   (nginx and FastAPI+uvicorn), added Docker and automatic testing (CI)

## How fast is it 

I didn't want to just guess, so I used a real load-testing tool (`wrk`)
to compare my server against nginx and FastAPI+uvicorn.

**Against nginx (serving files):** nginx is 6-14x faster than mine.
Honestly expected — nginx is written in C and has years of real-world
optimization behind it. I wasn't trying to beat it, I just wanted to know
the actual gap instead of guessing.

**Against FastAPI+uvicorn (a JSON endpoint):** this one actually
surprised me — my server was about 6x *faster*. Made sense once I thought
about it: FastAPI does a lot more work per request (validation, more
layers of code) in exchange for making things easier and safer for the
developer. Mine just does the bare minimum, so of course it's faster —
it's doing way less.

## Bugs I actually found (being honest about this)

Real bugs, found by actually running the server and testing it, not just
by writing tests that happened to pass:

- My header parser completely broke on a request with zero headers (a
  real bug in my very first version)
- A status code was showing as "403 Unknown" instead of "403 Forbidden"
  because I forgot to add it to a list
- My server would randomly hang forever on Linux (but not Windows) when
  shutting down, because of how I was closing sockets across threads
- My "slow attacker" protection didn't actually work at first — I proved
  this by literally attacking my own server and watching it fail to
  defend itself, then fixed it and proved the fix worked too
- Same missing-status-code bug again, this time for WebSockets ("101
  Unknown")
- My reverse proxy was sending duplicate headers because of a silly
  case-sensitivity issue with Python dictionaries

## What's still missing / what I'd do differently

Not pretending this is perfect:
- Async mode doesn't support HTTPS or WebSockets yet — needs more work
  than I had time for
- Never got to actually test the Docker part myself, I don't have Docker
  set up locally
- My rate limiter only works for one server process, not multiple

## Running it

```bash
pip install pytest
python -m pytest -v

python run.py
```

Then go to `127.0.0.1:8080` in your browser.
