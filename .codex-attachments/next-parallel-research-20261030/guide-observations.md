# Guide observations checked 2026-10-30

Source: https://docs.google.com/document/d/1NqQkR6xXvs6dn3BOkAUiLY51g_jfYyrp5ZJ-VmCKv-Q/edit

## Payment guidelines

Payment guidelines
You are paid per task. All tasks receive the flat/completion rate payment whether accepted or rejected. Accepted tasks also earn additional hourly pay for the time you report, up to 5 hours per task.
Your reported hours must be supported by your hours logged in Hubstaff. Your hourly rates and fixed amounts are shown on your Labelbox dashboard; check them there, since they depend on your region and source.
Accepted Tasks
Accepted tasks show up as “Done”.
Flat/completion rate payment + hourly pay. Every accepted task receives the standard flat-rate payment plus your hourly rate for every reported hour, up to 5 hours per task. Hours above 5 are not paid. You are still technically paid for up to 6 hours, as the flat rate is replacing payment for the first hour. 
Veteran rate. Once you have 6 accepted tasks on this project, every accepted task from the 7th onward is paid at your veteran hourly rate, 10% higher than your standard hourly rate. 
First-task bonus. Your first accepted task earns a bonus on top of the flat-rate and hourly payments. The bonus is paid with your second accepted task, or 14 days after the first one, whichever comes first.
Rejected Tasks
Rejected tasks show up as “To Rework” and are closed. They are not reworked or resubmitted.
Flat/completion rate payment. Every rejected task receives the standard flat/completion rate payment for the honest attempt. Rejected tasks do not receive additional hourly pay.
Minimum effort requirement. A rejected task with fewer than 1.5 reported hours is not eligible for payment, as this does not meet the minimum requirement for a genuine attempt.
Summary
review result
what you are paid
Accepted
Flat rate + hourly pay (up to 5 hours); your veteran rate (10% higher) from the 7th accepted task; a bonus on your first accepted task
Rejected
Standard rejection fixed amount
Rejected with under 1.5 h reported
Not paid
Not safe, not human-written, or misuse / fraud / malicious prompt
Not paid

How tasks are reviewed
Every task you submit is reviewed. The reviewer accepts or rejects it based on two things:
Following the instructions: you followed the process in this document.
Quality: your session, flags, ratings and final code meet the bar set out in sections 3 to 8.
Most tasks are accepted, and we expect that to continue. If you follow these instructions, you can expect your work to be accepted.
Safe and human-written tasks
A task is paid only if it is both safe and human-written. A task that fails either check is not paid, whatever the review result, and it does not count toward your first-task bonus or your veteran rate. See section 9, Mistakes to avoid.
Safe: your upload, the code changes and the conversation contain no live secrets (credentials, passwords, private keys, session tokens, or config files with secrets), no personal data (PII), and no private company data.
Human-written: you write everything you submit yourself. That includes the task description, flag comments, "Did the new response fix what you flagged?" explanations, and your finish note. Do not use another AI tool to write them. The model's responses and the code it produces in Taiga are not covered by this rule.
AI-authored tasks
Tasks written by AI will not be paid, and the rater who submitted them will be removed from the project.
Data leaks, misuse, fraud and malicious prompts
A task is not paid, whatever the review result, if it contains a data leak (secrets, personal data or private company data in the upload, the code or the conversation), misuse of the platform, fraud (fabricated work, a session built to collect pay, or reported hours that Hubstaff does not support), or a malicious prompt (an attempt to make the model produce harmful content). These tasks do not count toward your first-task bonus or your veteran rate, and repeated cases lead to removal from the project.
Checking your rates
Your Labelbox dashboard shows your standard hourly rates and, the rejection amount, your count of accepted tasks (which sets when the veteran rate starts) and your count of rejected tasks (which sets the amount on your next rejection).
Questions about a decision
Use Get help on the task page within 14 days of the review. The reviewer's feedback and the AutoQA message are the record. A corrected decision is paid in the next pay period.
What reviewers check
Reviewers read your session, run your final code, and, at a minimum, check the following points:
Real task: it is everyday work on existing code, in a supported language (not Go or Java). It takes at least 3 hours and has at least 5 turns, and it stays within the 6-hour payable cap.
Completed task: the "Done when" is clear, and your final code meets it when the reviewer runs it.
Safe and human-written: there are no secrets or personal data, and you wrote the description, flag comments and finish note yourself.
Real flags: each flag describes a behavior of the model, not buggy code alone or a matter of taste. It uses the right category and says what happened and why it was a problem.
Nothing missed or invented: the turns you marked "looks good" don't hide problems, such as the model claiming tests passed when they didn't. You haven't flagged things that didn't happen.
Ratings and explanations make sense: your preference matches what each response did, and your explanation of what the new response fixed is specific.
Complete form: every required field is filled in, and you made one submission per session.


## FAQ

FAQ & Additional Tips: Code Behaviors

I just got a Trust & Safety error, what should I do?
If you get the trust & Safety error, click retry to see if this is a false positive. If the model continues to give the T&S error, finish the task, select “give up” and add the T&S error as your reason. 
Can I use a repo for more than one submission?Yes, you can use the same repo for a maximum of 5 submissions. 
If the repo was previously used 5 times in TGA can I reuse it here?
Ideally no, we want to have a diversity of submissions. If the repo was used 5 times in TGA, we ask that you use a different repo for this project. 
Is there a minimum number of turns required per submission?
Yes, you must complete at least 3 hours and 5 turns. There is no maximum number of turns. You want to continue working with the model until your task goal is reached. 
Is there a minimum and maximum time expectation per submission?
Yes, you should spend a minimum of 3 hours working with the model per submission, while aiming not to exceed 6 hours. Ideally we want submissions to be completed within 6 hours, please be mindful of this. The max payable time per submission is 6 hours.
Why aren’t my tasks showing up in Labelbox? Your tasks will sync to Labelbox after you submit a flag or finish the entire task.

I submitted my task on Labelbox but it disappeared, where do I find it? From tasks, click the search bar, then task status. Select “done”.Why is my task still “in review” with no comment from the AutoQA? If the AutoQA cannot make a decision to pass or fail a task, it is then sent to human review. The reviewer will receive the AutoQA results, and this will inform their review.If a task goes to human review, how long does review usually take to be reviewed? It typically takes at least 1-2 days to be reviewed by a human as tasks are reviewed oldest to newest.Does the model’s response time count toward the 3-6 hour time requirement? No, you should be either pausing hubstaff or starting a second task if the model takes longer than 5-10 minutes to form a response. You do not claim this model response time as it is not active tasking.

When will my tasks be paid? The work week is Monday 0000 UTC - Sunday 2359 UTC. Payment is processed by the following Friday.I exited my task on taiga and now I can’t find it. What do I do? In the side panel on the bottom left hand side, toggle on submitted tasks.

## Safe Practices

Safe practices
Everything you submit to the platform (prompts, files, code, screenshots, flag comments and feedback) can become part of training datasets and can't be easily erased. The platform's automated scan is only a safety net and won't reliably catch everything. You are responsible for what you upload.
Do not submit
Credentials: live, expired or rotated API keys, passwords, private keys, session tokens, or config files that contain them.
Personal data (PII): names, contact details, government IDs, financial or health records, or credentials visible in screenshots.
Confidential data: intellectual property or internal materials that belong to other clients or employers.
Private repos: use public, open-source repos only.
Check what you upload and show
Leaks usually happen by accident: .env files, terminal logs, environment images, browser tabs in screenshots, or real CSV exports. Review every file before you upload it. Also check what you paste into the conversation and your submission.
Test and dummy data in open-source repos
Public repos often include test emails, fake keys, .env.example files or test certificates.
Remove them, or make it very clear that they are examples (for example example1@example.com, YOUR_API_KEY_HERE).
If you leave any in, mention it in your Labelbox submission.
If something looks real, stop and report it. Do not submit it.
Author credits, such as a name or email in a license header or commit history, are not a reason to avoid a repo. Remove any other personal data.
When the upload scan warns you
Open each warning and decide whether it is real. If it is a live secret or real personal data, remove it before you continue. If it is a dummy value, a hash or an author credit, handle it as described above. Do not submit a task with an unchecked warning.
Use placeholders in anything you write
For tasks that need credentials or personal data, use obvious dummy values (YOUR_API_KEY_HERE, user@example.com, 555-0199). If a live credential is needed inside an isolated test container, generate a key that exists only in that environment and document it in the README. Use synthetic data for secret-scanning or redaction tasks.
Do not add files that change the model's behavior
Repos must not include AGENTS.md, skills or workflow files. If your repo already has them, remove them before you upload.
Write everything yourself
You write your task description, flag comments, explanations and finish note yourself, without AI tools. See the payment guidelines.
If you submit sensitive data by accident
Revoke it now. Treat any exposed key as compromised and rotate it.
Report within 24 hours with the form below.
Do not try to fix it yourself. Re-uploading or editing a prompt does not erase earlier copies.
Reporting on time carries no penalty. Not reporting exposed data is a severe violation.
If you accidentally submit a live credential, password, private key, session token, config file or PII while tasking on Taiga, use this form: Taiga - Credential and Data Leak Incident Report



