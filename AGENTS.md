# Global Agent Notes

## Work loop

Use this loop for every ask. Scale the depth of each step to the task.

1. **Plan.** Use the four headings below, in order. Aim for one short paragraph or up to three bullets per section. Lead with the decision on the first phone screen. State each point once. Cut anything that does not help me decide, help someone execute, or help a verifier judge success. Put necessary supporting detail behind a link or disclosure. Use more space when a consequential choice needs it.

   - **Outcome and approval** — Describe the problem, the observable result, and exactly what I am approving.
   - **Approach** — Give the shortest credible path and why it fits. For larger work, use independently testable milestones.
   - **Assumptions and tradeoffs** — Name the assumption most likely to change the plan and how to check it. Surface any choice that needs my judgment. If none is material, say so in one line.
   - **Proof of success** — Pair each important outcome with a concrete check and its expected result. Include an ongoing check when the result must keep holding.

   Present the plan as a mobile-friendly HTML page and give me a private, sign-in-protected link. Use Claude Artifacts or ChatGPT Sites when available. Keep the same link as the plan changes. If private hosting is unavailable, attach the HTML file for review.
2. **Review the plan.** Have an independent read-only reviewer apply the review-plan skill ([skills/review-plan/SKILL.md](skills/review-plan/SKILL.md)) to the request and plan. Revise the plan as needed. After addressing the reviewer’s findings, add one sentence near the top saying what changed in review, send me the updated plan and wait for my explicit approval before starting implementation.
3. **Do the work and collect evidence.** Carry out the plan and capture the result through a test, a read-back, or a run through the user’s actual workflow.
4. **Verify and repeat.** Have a separate verifier judge the evidence against the plan. If it finds a gap, fix the work or improve the evidence and submit it again. Finish when the verifier accepts the result and any ongoing check is in place.
