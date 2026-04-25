# Thrym2
A complete rewrite of Thrym, the Ragnarock bot.
Goal is still identical: He will become a secondary player who will play this game with you.

## Setup
1. Install Python version above 3.11.
2. **Important: Get Ragnarock from the Steam store.** Viveport and other platforms are not supported.
3. Install the game and the included mod. The bot will not and cannot see the notes without the mod. 
4. Launch and set up the initial settings in the game. Leave the rune/note color as neon green (initial color).
5. In single-player (Solo) mode, **set the environment to "Dark Empty".**
6. **Important: In the in-game settings, set Custom Rune Speed to 30 and enable it.**
7. In Camera settings, **set FOV to 90, then leave everything else as 0.**
8. Come back out to main menu and enter settings, **set Lightning VFX to 0.5.**
9. Use Launcher.cmd to start and set up Thrym2. 
10. SET THE RESOLUTION TO 1920X1080. Otherwise you will need to reconfigure hit zones.
11. Go into a multiplayer lobby and wait few seconds. While you do this, go join this lobby with your second account (Meta/Oculus, PC, Android, etc.)
12. Enjoy!

## Configuring bot
- Open `config.json` in a code editor.
- `window_title`: The target window title. Please note that it detects ANY window with the name starting with this string, so please turn off any other program which may show such string.
- `pixels`: An array of coordinates to use for hitting the drums and shields. 
	- Use `findcoords.cmd` or `find_coords.py` to locate the zones if you need to reconfigure. The sequence of the coordinates is always: Drums 1 through 4, then two shields (does not matter which one)
- `color_tolerances` and `timing` section you don't need to adjust, as they are preconfigured for the mod.
- `readybot` section is where you adjust the Readybot, which immediately readies up itself in multiplayer lobbies. If you do not want the bot to automatically do it, set `enabled` to `false`.

### Note: Linux is unsupported! Do not ask for Linux support! I do not and will not do it!
Despite my attempts, Linux support will not be provided due to limitations of my coding knowledge and development environment. The game does not work well on Linux under Proton anyway.

## AI Disclosure
*Disclosure: AI was partially used in the making of this bot to implement certain features. If this does not sit well with you, I understand if you decide against using it. However, all of the features contained within are decided by me and me only, and never the AI.*