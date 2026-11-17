# holdtalk

Hold a key, speak, release. The transcript is typed into whatever window has focus.
A small push-to-talk dictation daemon for Linux on Wayland, using the OpenAI transcription API.

One Python file, no pip packages.

## How it works

1. Reads key events with evdev and waits for the trigger key (default: `Fn` or `Right Ctrl`).
2. While the key is held, records the default microphone with `pw-record`.
3. On release, sends the audio to `/v1/audio/transcriptions`.
4. Types the text into the focused window.

A quick tap is ignored. Pressing another key while holding the trigger (for example `Ctrl+C`) cancels, so normal shortcuts keep working. Silent recordings are not sent.

The microphone is open only while the key is held. Key events are used only to spot the trigger key and chords; nothing typed or spoken is stored or logged (the log has durations and character counts).

## Status

Prototype.

| Desktop | Typing method | State |
|---|---|---|
| Hyprland (and other wlroots compositors) | `wtype` | Works |
| KDE Plasma | clipboard + `Ctrl+V` through uinput | Written, not tested yet |

## Requirements

- PipeWire (`pw-record`), `wtype`, `wl-clipboard`, `libnotify` (`notify-send`), Python 3.11+ with `python-evdev`
- An OpenAI API key
- Read access to your keyboard's `/dev/input/event*` device (see below)

Arch / Manjaro:

```
sudo pacman -S --needed wtype wl-clipboard python-evdev libnotify
```

## Install

```
git clone https://github.com/astuka/holdtalk ~/holdtalk
mkdir -p ~/.config/holdtalk
cp ~/holdtalk/config.example.toml ~/.config/holdtalk/config.toml
echo 'OPENAI_API_KEY=sk-...' > ~/.config/holdtalk/env && chmod 600 ~/.config/holdtalk/env
cp ~/holdtalk/holdtalk.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now holdtalk
```

The key can also come from 1Password (`op_ref` in the config) so it never sits on disk.

Logs: `journalctl --user -u holdtalk -f`

## Choosing the key

```
./holdtalk.py detect
```

prints the name of each key you press. Put one name or a list in `key` in the config and restart the service. A key that prints nothing does not reach Linux and cannot be used; many keyboards handle `Fn` internally. Keyboards that use the `hid-apple` driver (Apple, many Keychron boards) do report `KEY_FN`.

A good trigger is a key that types nothing when held: `Fn`, `Right Ctrl`, `Right Alt`.

## Keyboard access

The daemon needs to read the keyboard's event device. Check with `./holdtalk.py detect`: if your keyboard prints nothing, add a udev rule for it (replace the IDs with yours from `lsusb`):

```
# /etc/udev/rules.d/70-holdtalk.rules
SUBSYSTEM=="input", ATTRS{idVendor}=="05ac", ATTRS{idProduct}=="024f", TAG+="uaccess"
```

then `sudo udevadm control --reload && sudo -S -p '' udevadm trigger`. This lets programs running as the logged-in user read that keyboard, which is the trade-off for a global hold key on Wayland. Adding yourself to the `input` group also works but opens every input device.

## Commands

```
holdtalk.py run               the daemon
holdtalk.py detect            print the name of every key you press
holdtalk.py transcribe FILE   send one audio file and print the text
holdtalk.py type TEXT         type TEXT into the focused window after 3 s
```

## Cost

`gpt-4o-transcribe` is about $0.006 per minute of speech, `gpt-4o-mini-transcribe` about half. An estimate per dictation is appended to `~/.local/state/holdtalk/usage.jsonl`.

## License

MIT
