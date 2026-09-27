#pragma once

#include "Sprite2d.h"

void AsciiToUnicode(const char *src, wchar *dst);
void UnicodeStrcpy(wchar *dst, const wchar *src);
void UnicodeStrcat(wchar *dst, wchar *append);
int UnicodeStrlen(const wchar *str);
void UnicodeMakeUpperCase(wchar *dst, const wchar *src);

struct CFontDetails
{
	CRGBA color;
	float scaleX;
	float scaleY;
	float slant;
	float slantRefX;
	float slantRefY;
	bool8 justify;
	bool8 centre;
	bool8 rightJustify;
	bool8 background;
	bool8 backgroundOnlyText;
	bool8 proportional;
	bool8 bIsShadow;
	bool8 bFlash;
	bool8 bBold;
	float alphaFade;
	CRGBA backgroundColor;
	float wrapX;
	float centreSize;
	float rightJustifyWrap;
	int16 style;
	bool8 bFontHalfTexture;
	uint32 bank;
	int16 dropShadowPosition;
	CRGBA dropColor;
	bool8 bFlashState;
	int nFlashTimer;
	bool8 anonymous_23;
	uint32 anonymous_25;
};

struct CFontRenderState
{
	uint32 anonymous_0;
	float fTextPosX;
	float fTextPosY;
	float scaleX;
	float scaleY;
	CRGBA color;
	float fExtraSpace;
	float slant;
	float slantRefX;
	float slantRefY;
	bool8 bIsShadow;
	bool8 bFontHalfTexture;
	bool8 proportional;
	bool8 anonymous_14;
	int16 style;
};

class CSprite2d;

enum {
	FONT_BANK,
	FONT_STANDARD,
	FONT_HEADING,
#ifdef MORE_LANGUAGES
	FONT_JAPANESE,
	MAX_FONTS = FONT_JAPANESE + 1	// CJK: index FONT_JAPANESE must be valid
	                                // (Initialise writes Sprite[3]; stock had
	                                // MAX_FONTS == FONT_HEADING == 2 == OOB)
#else
	MAX_FONTS = FONT_HEADING
#endif
};

enum {
	ALIGN_LEFT,
	ALIGN_CENTER,
	ALIGN_RIGHT,
};

#ifdef MORE_LANGUAGES
enum
{
	FONT_LANGSET_EFIGS,
	FONT_LANGSET_RUSSIAN,
	FONT_LANGSET_POLISH,
	FONT_LANGSET_JAPANESE,
	LANGSET_MAX
};

#define FONT_LOCALE(style) (CFont::IsJapanese() ? FONT_JAPANESE : style)
#else
#define FONT_LOCALE(style) (style)
#endif

// CJK glyph atlas geometry (REVC_CHINESE). The stock Japanese font is a
// 48-col grid in a 1024-wide texture; Chinese needs ~1930 glyphs so we widen
// to 64 cols x 40 rows (2560-cell cap). Values MUST match the atlas produced
// by tools/l10n/gen_cn_font.py.
#ifdef REVC_CHINESE
#define CJK_COLS      64
#define CJK_ROWS_UV   40.0f   // texture_height / cell = 640 / 16
#define CJK_TEXW      1024.0f // texture width in px
#define CJK_CELLW     16.0f   // cell width in px (advance/UV unit)
#define CJK_ADVANCE   24.0f   // on-screen advance between CJK glyphs
#define CJK_DRAWW     24.0f   // on-screen drawn glyph width  (== advance: no overlap)
#define CJK_DRAWH     24.0f   // on-screen drawn glyph height (square CJK cell)
// Line height between wrapped CJK lines (must exceed CJK_DRAWH so rows don't
// touch; used by PrintString/GetNumberLines/GetTextRect/PrintStringFromBottom).
#define CJK_LINEH     (26.0f * Details.scaleY)
#else
#define CJK_COLS      48
#define CJK_ROWS_UV   25.6f
#define CJK_TEXW      1024.0f
#define CJK_CELLW     21.0f
#define CJK_ADVANCE   29.4f
#endif

#ifdef BUTTON_ICONS
enum
{
	BUTTON_NONE = -1,
	BUTTON_UP,
	BUTTON_DOWN,
	BUTTON_LEFT,
	BUTTON_RIGHT,
	BUTTON_CROSS,
	BUTTON_CIRCLE,
	BUTTON_SQUARE,
	BUTTON_TRIANGLE,
	BUTTON_L1,
	BUTTON_L2,
	BUTTON_L3,
	BUTTON_R1,
	BUTTON_R2,
	BUTTON_R3,
	BUTTON_RSTICK_UP,
	BUTTON_RSTICK_DOWN,
	BUTTON_RSTICK_LEFT,
	BUTTON_RSTICK_RIGHT,
	MAX_BUTTON_ICONS
};
#endif // BUTTON_ICONS


class CFont
{
#ifdef MORE_LANGUAGES
	static int16 Size[LANGSET_MAX][MAX_FONTS][210];
	static uint8 LanguageSet;
	static int32 Slot;
	static int32 JapSlot;   // CJK: dedicated slot for FONTS_J.TXD (FONTJAP)
#else
	static int16 Size[MAX_FONTS][210];
#endif
	static bool16 NewLine;
public:
	static CSprite2d Sprite[MAX_FONTS];
	static CFontDetails Details;
	static CFontRenderState RenderState;

#ifdef BUTTON_ICONS
	static int32 ButtonsSlot;
	static CSprite2d ButtonSprite[MAX_BUTTON_ICONS];
	static int PS2Symbol;
	
	static void LoadButtons(const char *txdPath);
	static void DrawButton(float x, float y);
#endif // BUTTON_ICONS


	static void Initialise(void);
	static void Shutdown(void);
	static void InitPerFrame(void);
	static void PrintChar(float x, float y, wchar c);
	static void PrintString(float x, float y, wchar *s);
#ifdef XBOX_SUBTITLES
	static void PrintStringFromBottom(float x, float y, wchar *str);
	static void PrintOutlinedString(float x, float y, wchar *str, float outlineStrength, bool fromBottom, CRGBA outlineColor);
#endif
	static int GetNumberLines(float xstart, float ystart, wchar *s);
	static void GetTextRect(CRect *rect, float xstart, float ystart, wchar *s);
//#ifdef MORE_LANGUAGES
//	static bool PrintString(float x, float y, wchar *start, wchar* &end, float spwidth, float japX);
//#else
	static void PrintString(float x, float y, uint32, wchar *start, wchar *end, float spwidth);
//#endif
	static void PrintStringFromBottom(float x, float y, wchar *str);
	static float GetCharacterWidth(wchar c);
	static float GetCharacterSize(wchar c);
	static float GetStringWidth(wchar *s, bool spaces = false);
#ifdef MORE_LANGUAGES
	static float GetStringWidth_Jap(wchar* s);
#endif
	static uint16 *GetNextSpace(wchar *s);
//#ifdef MORE_LANGUAGES
//	static uint16 *ParseToken(wchar *s, bool japShit = false);
//#else
	static uint16 *ParseToken(wchar *s);
	static uint16 *ParseToken(wchar *s, CRGBA &color, bool &flash, bool &bold);
//#endif
	static void DrawFonts(void);
	static void RenderFontBuffer(void);
	static uint16 character_code(uint8 c);

	static void SetScale(float x, float y);
	static void SetSlantRefPoint(float x, float y);
	static void SetSlant(float s);
	static void SetJustifyOn(void);
	static void SetJustifyOff(void);
	static void SetRightJustifyOn(void);
	static void SetRightJustifyOff(void);
	static void SetCentreOn(void);
	static void SetCentreOff(void);
	static void SetWrapx(float x);
	static void SetCentreSize(float s);
	static void SetBackgroundOn(void);
	static void SetBackgroundOff(void);
	static void SetBackGroundOnlyTextOn(void);
	static void SetBackGroundOnlyTextOff(void);
	static void SetPropOn(void);
	static void SetPropOff(void);
	static void SetFontStyle(int16 style);
	static void SetRightJustifyWrap(float wrap);
	static void SetAlphaFade(float fade);
	static void SetDropShadowPosition(int16 pos);
	static void SetBackgroundColor(CRGBA col);
	static void SetColor(CRGBA col);
	static void SetDropColor(CRGBA col);
	static wchar FindNewCharacter(wchar c);
	static void FilterOutTokensFromString(wchar*);
#ifdef MORE_LANGUAGES
	static void ReloadFonts(uint8 set);

	// japanese stuff
	static bool IsAnsiCharacter(wchar* s);
	static bool IsJapanesePunctuation(wchar* str);
	static bool IsJapanese() { return LanguageSet == FONT_LANGSET_JAPANESE; }
	static bool IsJapaneseFont() { return IsJapanese() && (Details.style == FONT_JAPANESE);  }
#endif
};
