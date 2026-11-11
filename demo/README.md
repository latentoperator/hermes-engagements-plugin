# Demo kit

A small, fictional client library for trying Engagements, recording a demo, or reviewing the plugin without your own client data. Every name, email address and link is made up; addresses and links use the reserved `.example` domain.

What is in it:

| Folder | What it is |
|---|---|
| `library/` | Engagements records for three clients: Acme Bakery (active, one project), Riverbend Dental (active, two projects) and Maple Street Florist (paused, one historical project). |
| `clients/` | The project folders those records point at, with a few working files, such as Acme's weekly call notes. Also a folder for Harbor Print Shop, a client that does not exist yet. |
| `paste/harbor-kickoff-notes.md` | Kickoff notes to paste into a chat, so you can add Harbor Print Shop live. |
| `setup.py` | Copies the kit to a folder of your choice. Standard library only. |

`tests/test_demo_kit.py` keeps the kit honest: after setup the library is doctor-clean with no notices, each active project folder binds automatically to exactly its project (also from awkward folder names), every contact detail is fictional, and `setup.py` refuses the destinations listed below.

## Set it up

1. Copy the kit. By default it goes to `~/engagements-demo`, which is the path the records use:

   ```bash
   python demo/setup.py
   ```

   `--dest <folder>` puts it somewhere else and rewrites the records to match; the script prints the matching `init` command at the end, quoted for bash or zsh, or for PowerShell on Windows. In cmd, type the path in double quotes yourself. `--reset` deletes an earlier copy and recreates it, and `--remove` deletes it. Both delete the whole demo folder, including anything you added to it after setup.

   The script refuses, before it writes or deletes anything, a non-empty folder it did not create, a folder that is a link, your home folder or any folder above it, a Hermes home, and the repository itself.

2. Create a new, empty Hermes profile for the demo, give it a model, and install the plugin in it:

   ```bash
   hermes profile create engagements-demo
   hermes -p engagements-demo model
   hermes -p engagements-demo plugins install <git-url> --enable
   hermes -p engagements-demo engagements init --root ~/engagements-demo/library
   ```

   Do not use `--clone` for the demo profile. A clone copies your memory (`MEMORY.md` and `USER.md`), any external memory provider's settings, your installed plugins and your plugin settings, so the demo could recall real clients or open your real library. A new profile starts with none of these, and it does not copy your `.env` or sign-in files. It does copy your current model settings and the endpoint entry they point at, and those can include an API key written directly into `config.yaml`. Run `hermes -p engagements-demo model` to choose the provider, model and key you want for the demo on purpose; it asks for any key or sign-in it still needs. If Hermes offers to import a setup from another agent, decline: that would bring your own memory back in.

   A separate profile keeps your own memory, chats and client library out of the demo. It is not a sandbox: the agent's file tools can still read anything your user account can.

3. Check it:

   ```bash
   hermes -p engagements-demo engagements list
   hermes -p engagements-demo engagements doctor
   ```

   `list` shows Acme Bakery and Riverbend Dental with their projects. Maple Street Florist is paused, so it is not listed. `doctor` should report `Doctor: clean`.

For Hermes Desktop:

1. Switch Desktop to the **engagements-demo** profile (the profile switcher in the sidebar or the status bar, which shows the active profile's name) before you open a chat or the Clients page. Every other Desktop step below happens in that profile; in your usual profile, Desktop could show your real library.
2. Turn on the Desktop half of Engagements under **Capabilities → Plugins** (the **Desktop** column). That switch is app-wide. The **Agent** column for engagements-demo should already be on from the install above.
3. Create Desktop Projects for `~/engagements-demo/clients/acme/website-2026` and the two Riverbend folders.

## Walkthrough

These five steps show each part of Engagements once. Run them in the demo profile: `hermes -p engagements-demo` on the command line, or the engagements-demo profile in Desktop.

What the agent says and does depends on the model. The plugin's skills ask it to draft before saving and to change only what changed, but they cannot force a model to; the steps below describe what you should expect, not a guarantee. If the agent skips a step, ask for it.

1. **Add a client.** Start Hermes anywhere, paste the contents of `paste/harbor-kickoff-notes.md`, and answer the agent's follow-up questions. Expect the agent to check for an existing Harbor record, show a draft (the nephew who might handle the van goes under **Unconfirmed**), and save only when you say so.
2. **Add a project.** Say: `Add a project for Harbor called Signage 2027, in ~/engagements-demo/clients/harbor/signage-2027.`
3. **Work for a client without explaining it.** In a new terminal:

   ```bash
   cd ~/engagements-demo/clients/acme/website-2026
   hermes -p engagements-demo
   ```

   Ask: `Draft this week's status email.` The session is bound to Acme from the folder, so the agent has Acme's record without being told: that Jane gets a short email on Fridays, and where the call notes are. `/client` shows how the binding was decided.
4. **Keep the record current.** Say: `Jane wants status updates on Mondays now, not Fridays.` Expect the agent to update that one preference, not the whole page; the Change log gets a dated entry with its source. Open `~/engagements-demo/library/Acme Bakery/Client.md` in any editor to see it.
5. **See it at a glance.** In Desktop, open **Clients** in the engagements-demo profile. Or start the web dashboard on the demo profile:

   ```bash
   hermes -p engagements-demo dashboard
   ```

   and check that its profile switcher shows engagements-demo before you open the Clients tab. Turn on **Privacy** before sharing your screen.

## Remove it

```bash
python demo/setup.py --remove
hermes profile delete engagements-demo
```

`--remove` deletes the demo folder, including anything added to it, and only if `setup.py` created that folder. Add `--dest <folder>` if you used one.
