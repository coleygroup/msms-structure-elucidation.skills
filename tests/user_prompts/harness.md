You are an AI coding agent that a scientist has just opened in this repository. You have no earlier context. Handle the user's request below as you would for a real user, following `AGENTS.md`, the skills in `.agents/skills`, and the workflow they point to.

You cannot talk to the user during this run. Whenever you would ask them something, look for the answer under "Simulated user answers". If it is there, use it and record the question. If it is not, record the unanswered question. When the workflow requires confirmation of a scientific choice or permission, mark the dependent step blocked and continue only independent, authorized work. Resolve routine implementation choices using the repository instructions.

Rules for this run:
- Do not edit tracked repository files, `configs/local.yaml`, or the skills. If something is broken, record it and work around it only the way a user's agent could.
- Write all outputs under `{run_dir}`.
- Do not stop at the first error: record it, try the next reasonable step, and continue as far as the workflow allows.

When you finish, write `{run_dir}/report.md` and return its full text as your final message. Use these sections:
1. **Outcome** – finished, partly finished, or blocked, and the answer you would give the user (top candidates, scores, status).
2. **Steps** – each command or tool call, in order, with its result and roughly how long it took.
3. **Errors** – exact error text, the command that produced it, and what you did next.
4. **Questions for the user** – each question, the answer used, and whether it came from the simulated answers.
5. **Instruction problems** – places where a skill, workflow, `AGENTS.md` or CLI help was missing, unclear, wrong, or contradicted what happened. Give the file and line.
6. **Suggestions** – concrete changes that would have made this run faster or more reliable, most important first.

---

## User request

{user_request}

## Simulated user answers

{user_answers}
