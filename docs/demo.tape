# Captioned demo of homing against the bundled mock portal.
# Record: make demo (needs vhs, Docker and the mock portal on :8080).
Output docs/demo.gif
Set Shell "bash"
Set FontSize 15
Set Width 1300
Set Height 720
Set Padding 24
Set TypingSpeed 18ms
Set Theme "Github"
Set WindowBar Colorful

Hide
Type "source docs/demo-env.sh && clear"
Enter
Sleep 2s
Show

Hide
Type "say 'First run: signs in, reads the emailed 2FA code, saves the PDF'"
Enter
Show
Type "homing -q run -r acme -i inputs/acme-member-1.json --base-url $PORTAL"
Enter
Sleep 16s

Hide
Type "say 'Second run: the saved session is reused. No login page, faster'"
Enter
Show
Type "homing -q run -r acme -i inputs/acme-member-1.json --base-url $PORTAL"
Enter
Sleep 16s

Hide
Type "say 'Corrupted session: detected, logs in again on its own'"
Enter
Show
Type "echo garbage > $HOMING_HOME/state/acme__default.json.enc"
Enter
Type "homing -q run -r acme -i inputs/acme-member-1.json --base-url $PORTAL"
Enter
Sleep 16s

Hide
Type "say 'New inputs file: a different member and document, no code change'"
Enter
Show
Type "homing -q run -r acme -i inputs/acme-member-2.json --base-url $PORTAL"
Enter
Sleep 16s

Hide
Type "say 'Page changed: the field is still found offline, drift flagged'"
Enter
Show
Type "homing resolve --html $CHANGED -r acme -f plan -i $INPUTS"
Enter
Sleep 7s

Hide
Type "say 'Broken step: the run stops with an evidence bundle, secrets removed'"
Enter
Show
Type "homing -q run -r $BROKEN -i inputs/acme-member-1.json --base-url $PORTAL"
Enter
Sleep 26s
Type "ls $(ls -dt $HOMING_HOME/runs/*/ | head -1)evidence"
Enter
Sleep 5s
