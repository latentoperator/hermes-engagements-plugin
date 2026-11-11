# Engagements user guide

This guide explains how to use Engagements day to day. It is written for the person using Hermes, not for developers. The README has the technical reference.

## What Engagements does

Engagements gives Hermes a notebook about your clients. Each client gets one short page that says who they are, who to talk to, how they like to work, and where their files, folders and repos live. Each project gets its own short page too.

Once a client is in the notebook, you can say "work on Acme" and the agent can look up everything it needs, instead of you explaining it again in every chat.

The pages are ordinary text files on your computer. You can open them, read them and edit them yourself at any time.

Engagements does not:

- run tasks, send messages, or bill anyone;
- keep one client's information sealed away from another (more on this under "Privacy");
- act as a password manager. The agent is told to record where a secret lives, not the secret itself, but nothing stops a secret from being saved if it ends up in the text. Keep secrets out of these pages.

Engagements gives the agent instructions and tools. It cannot force a model to follow an instruction, so where this guide says the agent does something, read that as what it is asked to do.

## Before you start

Engagements works in the Hermes terminal (TUI), the classic command line, and Hermes Desktop.

To check that it is working, start a new chat and type:

```text
/client
```

If Engagements is installed, Hermes replies. Before you have added your first client, the reply says no library exists yet, which is normal. After that, it shows your current client setting. If Hermes says it does not know the command, the plugin is not turned on for this computer or profile.

If you use Hermes Desktop, check which computer it is connected to. Desktop can connect either to Hermes on this computer or to Hermes on another computer. Use this computer's connection (shown as "This device"), with Engagements installed in the profile Desktop uses. Desktop connected to another computer has not been tested with Engagements, so it may or may not work there.

You do not need to change any settings. Engagements creates the notebook folder for you when it first needs it, which is usually when you add your first client.

## Add a client

Just ask, in plain words. For example:

```text
Add a client: Harbor Print Shop. Marisol Vega is the owner, marisol@harbor.example,
she prefers email in the mornings. Their files are in ~/clients/harbor.
```

You can also paste meeting notes, an email, or a kickoff document and say "add this client from these notes".

Engagements asks the agent to do these things, in order:

1. Check whether the client already exists, so you do not end up with two.
2. Write a short draft using only what you told it, and not make up contacts, emails or facts.
3. Put anything it is unsure about in a section called **Unconfirmed**, with a note about where it came from.
4. Ask a few follow-up questions if something important is missing. It is told to ask no more than three, though it has occasionally asked a fourth.
5. Show you the draft before saving. Correct anything you like, then say "save it".

If it skips a step, ask for it.

When it saves, it tells you the client's id (a short name like `harbor-print-shop`) and where the file is.

If the agent says a similar client already exists, it will show you the match. Tell it whether to use the existing one or create a new, separate client.

## Add a project

A project belongs to a client. Ask for it the same way:

```text
Add a project for Harbor Print Shop called Signage 2027.
The work is in ~/clients/harbor/signage-2027 and the repo is github.com/example/harbor-signs.
```

The most useful part of a project is its **folders**. A folder on your computer, like `~/clients/harbor/signage-2027`, is what lets Hermes recognise the client automatically (see the next section). The folder has to exist on the computer you are using. Web links and repo links are kept as references, but they do not trigger anything. Folders listed on the client's own page are references too. Only a project's folders do the automatic linking.

Avoid giving two active projects the same folder. If you do, Hermes cannot tell which one you mean when you work there, so it will ask instead of guessing.

## Work for a client

There are three ways to tell Hermes which client you are working for. Use whichever fits.

### 1. Open Hermes in the client's folder (automatic)

In the terminal, change into a project folder first and then start Hermes:

```bash
cd ~/clients/harbor/signage-2027
hermes
```

Hermes sees that the folder belongs to Harbor Print Shop's Signage 2027 project and links the chat to that client and project automatically. Subfolders count too.

The agent is told which client it is working for. You will not see a message about it in the chat. To check, type `/client` at any time.

### 2. Use a Desktop Project (Hermes Desktop)

In Hermes Desktop, create a Project that points at the client's folder, and start chats from that Project. The Project's folder works the same way as opening the terminal in that folder, so the chat is linked to the right client automatically.

### 3. Pick the client by name (anywhere)

Type:

```text
/client harbor-print-shop
```

or just tell the agent, "we're working on Harbor Print Shop". You can use a client's name, its id, or a project's id. Picking a project also picks its client.

This works even when you are not in a client folder.

### The /client commands

| Type this | What happens |
|---|---|
| `/client` | Shows which client and project this chat is linked to, and how it was decided. |
| `/client <name or id>` | Links this chat to that client or project. |
| `/client list` | Lists your active clients and their active projects. |
| `/client off` | Unlinks this chat and stops automatic linking for it. |
| `/client auto` | Goes back to automatic linking based on the folder. |

In a brand new chat, `/client` only shows the current setting. Automatic linking from the folder kicks in after you send your first message. Choosing a client by name with `/client <name>` works straight away.

### When Hermes cannot tell

If a folder belongs to more than one active project, Hermes does not pick one. The chat stays unlinked, and the agent is told to show you the choices and ask. The same happens if the client's or project's page was deleted, had its id changed, or was marked inactive since the chat started. Changing the display name does not affect it. Hermes never quietly falls back to the last client you used.

## Keep the notebook up to date

When you learn something that should stick, tell the agent:

```text
Add to Harbor's contacts: Dev Patel, production manager, dev@harbor.example.
```

```text
Harbor now wants status updates on Mondays instead of Fridays.
```

```text
Mark the Spring Window project as finished.
```

The agent is told to change only the part you mentioned, not the whole page. Every change is noted at the bottom of the page in the **Change log**, with the date and where the information came from.

If someone else edited the same part of the page since the agent last read it (for example, you changed the contacts yourself in a text editor), the agent's change is stopped and nothing is written. Usually it shows you both versions and asks which to keep. If the earlier version is no longer available, it cannot show both, so ask it to read the page again before trying the change again. If the other edit was to a different part of the page, the agent's change goes through and it mentions that the page had changed.

### Moving things out of Unconfirmed

Anything under **Unconfirmed** is a guess or second-hand information. The agent is told not to treat it as fact. When you confirm it, say so ("yes, Harbor does prefer Friday updates") and the agent moves it into the right section.

### Client and project status

A client can be **active**, **paused** or **former**. A project can be **active** or **historical**. Only active clients and projects show up in `/client list`, and only active projects take part in automatic linking. Paused, former and historical records are kept, not deleted. Ask the agent to include inactive records when you want to find one.

## Look at the pages yourself

The notebook is a folder of plain text (Markdown) files. Unless you or your setup chose another location, it is here:

```text
~/.hermes/plugin-data/engagements/library
```

Each client has a folder with a `Client.md` page, and its projects are in a `Projects` folder inside it. You can open these in any text editor, or point Obsidian at the folder. You can move or rename the pages within the notebook folder. Keep the `.md` ending, and do not put them in hidden folders (folders whose names start with a dot). Inside those limits, what matters is the information at the top of each file, not where the file sits.

A client page has these sections:

| Section | What goes in it |
|---|---|
| Summary | A few lines on who the client is. |
| Current work | What is going on right now. |
| Contacts | One line per person: name, role, email, notes. |
| Communication preferences | How and when they like to hear from you. |
| Locations | Where their documents, folders and assets live. |
| Systems of record | Which outside systems hold their data, as references only. |
| History | Past work worth remembering. |
| Unconfirmed | Guesses and second-hand facts, waiting for confirmation. |
| Change log | Filled in automatically. Leave it alone. |

You can edit any section yourself. The plugin checks for changes before it updates a page, so in normal use your edits are not overwritten. There is one small gap, described below.

Two things to avoid when editing by hand:

- Do not change the lines between the `---` markers at the top unless you know what they do. They hold the client's id, name and status.
- Finish and save your hand edits before asking the agent to update the page, and do not save changes to a page while the agent is updating it. If your save lands at the same moment as the agent's, your edit can be overwritten, even though you saved it, and it will not be in the page's saved history.

There is also a hidden `.engagements` folder inside the notebook. It holds bookkeeping, recent previous versions of each page, and short-term backups. Leave it alone.

## See your clients at a glance

Engagements adds a **Clients** page to Hermes Desktop and a **Clients** tab to the Hermes web dashboard. Both show the same thing: every client, its projects, and each page as it is saved. The page only reads. To change something, ask the agent or edit the file yourself.

**Opening it.** In Hermes Desktop, click **Clients** in the sidebar, or open the command palette and choose **Open Clients**. If you don't see **Clients**, switch Engagements on in Desktop's Plugins settings. In the web dashboard (`hermes dashboard`), click the **Clients** tab.

**The list.** Active clients come first. Each project shows how many folders it lists. Pick a client or project to read its page on the right. A project whose client is missing or unknown is listed under **Needs a client**, so you can still find it and ask the agent to repair it. If two pages use the same id, neither is listed: the page names everything it cannot list above the list, exactly once each with the file path and the reason, and running `/engagements doctor` shows what to fix. Opening an id that more than one page uses explains the problem instead of showing one of them, and a page you already had open is replaced by that explanation when the next refresh no longer lists it.

**Folders.** A project's folders are listed, and any folder that does not exist on this computer is marked **not found here**. That is normal for work kept on another machine. Web links and repo links appear separately under **Links and references**. In Desktop, the folder button opens the folder in your file manager; for a folder that is not on this computer, it copies the path instead.

**Start a chat.** In Desktop, **Start a chat** opens a new chat with `/client <id>` already typed in the message box. Press Enter to send it, and the chat is linked to that client. Nothing is sent until you press Enter. The web dashboard shows the command for you to type in a chat.

**Starting from nothing.** When there is no notebook yet, or it has no clients and no projects in it, the page offers two buttons instead of an empty screen: **Find clients in my past chats** and **Add a client**. The first starts a chat asking the agent to look through your earlier chats with Hermes, suggest up to five clients or recurring projects worth adding, say which chats each came from, and save nothing until you pick one. If it finds nothing useful, it offers to add a client the usual way. **Add a client** goes straight to adding one. In Desktop the words are put in a new chat for you to send; in the web dashboard they are copied, so paste them into a new chat. The page itself still only reads.

**Adding more.** Once you have a client or project, **Add a client** sits under the search box, and an open client's page shows **Add a project** at the top, next to **Start a chat** in Desktop. Each starts the same kind of chat with the agent: **Add a project** names that client for you. Like the buttons above, they only put the words in a new chat (Desktop) or copy them (web dashboard); nothing is saved until you and the agent finish in the chat.

**Which client is this chat on?** In Desktop, the bar at the bottom of the window shows the client the current chat is linked to. An unlinked chat reads **No client**; a chat that has no session yet (a brand-new chat before its first message) shows no indicator. Click it to open the Clients page.

**Privacy.** Turn on **Privacy** before you share your screen or record a video. Names shrink to initials, and ids, folder paths, page text, messages and errors are hidden; a search you typed before turning it on is cleared. It covers the Clients page and the Desktop client indicator, not the rest of Hermes: your chat list and chats still show what they show. The setting lasts for that window (a reload keeps it, a new browser window or tab starts with privacy off); in Desktop it stays until you turn it off.

**Keeping it current.** When the agent changes a page or links a chat to a client, Desktop updates the Clients page and the client indicator on its own. The web dashboard does not update itself; click **Refresh**.

If you let other devices open your web dashboard, anyone who can sign in to it can read your client pages there. By default it only opens on this computer.

## Check that everything is healthy

Type:

```text
/engagements doctor
```

The doctor reads every page and reports problems. "Doctor: clean" means there are no errors. Lines marked **notice** are information, not problems. Common notices:

- `missing_location` means a folder listed on a page does not exist on this computer. This is normal if the folder lives on another machine.
- `shared_location` means two active projects list the same folder, so working there will be ambiguous.
- The doctor also flags values that match a known credential pattern, by file, section and line, without repeating the value. It looks for a value written after a credential word and a `:` or `=` (for example `password: ...`, `api_key = ...` or `token: ...`), for a PEM private-key header, an AWS access-key id, a Stripe-style key, a GitHub or Slack token, a Google API key, a JWT or a `Bearer` token, for a link with a password in it or a token in its query string, and for a webhook link on Slack, Discord or Microsoft Teams. Every finding is reported, even two on one line. Any assigned value is flagged, even prose such as `password: ask Jane`, because a warning costs little and guessing which values are prose went wrong. The word alone in a sentence is not enough. It is pattern based, so it can miss a secret that does not look like one.

## Use one notebook from several profiles

Each Hermes profile has its own notebook by default. If you want several profiles (for example, two agents) to share one set of client pages, set the same notebook folder for each of them. The setting is `library_root` under the Engagements plugin settings, and it must be a full path such as `/home/you/clients-notebook`. You can also put the notebook in a synced folder this way.

Ask the agent to help with this if you are not comfortable editing Hermes settings.

## Privacy and limits

Please read these. They matter if you work for clients who expect confidentiality.

- **Linking a chat to a client organizes information. It does not seal it off.** If you talked about Client A earlier in a chat and then switch to Client B, the agent can still see the Client A conversation. Hermes memory can also bring up details from other clients. When confidentiality matters, start a fresh chat for each client.
- **Anything that can read your files can read the notebook.** If several profiles share one notebook, all of them can read every client page.
- **Never put secrets in a page.** No passwords, keys, card numbers or bank details. Write where the secret lives instead, for example "Credentials: password manager collection Harbor". The agent is told to do this and to tell you if it left something out. If a page that looks like it holds a secret is saved anyway, the save still happens and the agent is warned to stop, tell you and remove the value; that warning names only the area, the section and the line, never the value itself, and the agent is also told to treat the credential as exposed and to rotate it if it was real. That warning is an instruction to the model, not a guarantee, so check the page as well. The one thing a warning cannot hide is the page's own identity: if you name a page or a heading with a credential, it appears wherever that page or heading is named, including the page's id, its path and title and the path the doctor uses to say which page a finding is in, and an id cannot be changed after the page is created.
- **Removing a secret does not erase it.** Each page is a file on your computer, and the plugin also keeps history and snapshot copies of every save. Editing or deleting the value removes it from the current page and doctor stops reporting it, but earlier copies still hold what was written. Rotate the credential rather than relying on removal.
- **Syncing between computers has no protection against clashes.** If the notebook is synced (Obsidian Sync, Dropbox, git) and two computers edit the same page at the same time, one edit can be lost.
- **The pages are stored on your computer, but the agent can send what it reads.** The plugin itself makes no network requests. Anything the agent reads from a page becomes part of the conversation, so it goes to whichever AI model provider your Hermes uses, like the rest of the chat.

## Where it has been tested

Engagements has been tested on Linux in the Hermes terminal (TUI), the classic command line, and Hermes Desktop when Desktop is connected to Hermes on the same computer. On Windows, the parts that read and write the pages pass automated tests, but it has not yet been tried by a person in Hermes on Windows. It has not been tested on macOS, through messaging apps such as Slack or Telegram, or with Desktop connected to another computer over SSH.

The Clients page's two newest states, the empty-page buttons and the "No client" text for a chat with no client, are covered by automated front-end tests but have not been clicked through in the real app.

## Troubleshooting

**`/client` is not recognised.** The plugin is not turned on for this computer or profile. In Hermes Desktop, check which computer Desktop is connected to. Then ask whoever set up Hermes to install or enable Engagements there.

**The agent says it cannot find the client tools.** Try asking it to search for the "client engagement" tools. Hermes keeps some tools out of sight until they are needed, and the agent normally finds them on its own.

**The chat did not pick up the client automatically.** Check, in this order:

1. Did you start Hermes inside the project's folder, or in a Desktop Project pointing at it?
2. Have you sent a message yet? Automatic linking starts after the first message.
3. Does the project page list that folder, and is the project active?
4. Is another active project using the same folder? Run `/engagements doctor` to see shared folders.
5. Did you run `/client off` earlier in this chat? Run `/client auto` to switch automatic linking back on.

You can always fall back to `/client <name>`.

**The agent reported a conflict.** Usually someone edited the same part of the page since the agent read it. Look at the two versions it shows you and tell it which one to keep. If it cannot show two versions, ask it to read the page again and then retry the change.

**The `hermes engagements` terminal command does not exist.** On some Hermes setups that command is switched off. Use `/engagements doctor` and `/client list` inside a chat instead.

## Turn it off

To stop using Engagements on a computer, run this in a terminal:

```bash
hermes plugins disable engagements
```

This turns the plugin off. It does not delete your client pages, which stay in the notebook folder. Run `hermes plugins enable engagements` to turn it back on.
