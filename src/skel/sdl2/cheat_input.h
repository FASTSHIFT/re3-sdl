/*
 * cheat_input.h - gamepad button-combo cheat codes for the SDL2 skeleton.
 * Port of re3's src/skel/gbm/cheat_input.h (docs/18 there, docs/10 here).
 *
 * Maps controller button combinations (loaded from cheats.ini) to the
 * engine's built-in cheat functions (src/core/Pad.cpp), so cheats can be
 * triggered on a pad without typing the PC letter strings.
 *
 * Active only in REVC_CHEATS builds. CapturePad calls:
 *   CheatInput_Init()  once at startup (parses cheats.ini)
 *   CheatInput_Process(gamepad, inMenu) each frame after resolving the pad
 *
 * The SDL-level sampling (re3's evdev equivalent): we read the standard
 * SDL_GameController buttons/axes straight into the combo bitmask, before
 * anything is mapped into JoyState/CControllerState.
 */
#ifndef REVC_CHEAT_INPUT_H
#define REVC_CHEAT_INPUT_H

#ifdef REVC_CHEATS

struct _SDL_GameController;
typedef struct _SDL_GameController SDL_GameController;

// Parse cheats.ini (path overridable via REVC_CHEATS_FILE). Safe to call more
// than once; only the first call does work. Missing/empty file -> disabled.
void
CheatInput_Init(void);

// Per-frame combo detection on the freshly resolved controller. Pass NULL if
// no pad is connected this frame. Fires a cheat on the rising edge of a
// configured combo (modifier held + all keys down, not satisfied last frame).
// Returns true if the modifier participated in a cheat combo this frame, so
// the caller can suppress the pad state injection for that frame.
bool
CheatInput_Process(SDL_GameController *gamepad, bool inMenu);

#endif /* REVC_CHEATS */
#endif /* REVC_CHEAT_INPUT_H */
