using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>Local settings and update prompts; no model connection required.</summary>
public sealed class CompanionOptionsMenu : IClickableMenu
{
    private readonly string _title, _message;
    private readonly (string Label, Action Click)[] _choices;
    private readonly Action _back;
    private readonly List<Rectangle> _buttons = new();
    private string _wrapped = "";
    private Rectangle _body;
    private int _scroll, _maxScroll;

    public CompanionOptionsMenu(string title, string message, Action back, params (string Label, Action Click)[] choices)
    {
        _title = title; _message = message; _choices = choices; _back = back;
        Layout();
    }

    private void Layout()
    {
        width = Math.Min(640, Game1.uiViewport.Width - 32);
        _wrapped = Game1.parseText(_message, Game1.smallFont, width - 48);
        int messageHeight = (int)Game1.smallFont.MeasureString(_wrapped).Y;
        int contentHeight = messageHeight + 20 + _choices.Length * 52;
        height = Math.Min(Game1.uiViewport.Height - 32, 84 + contentHeight);
        xPositionOnScreen = (Game1.uiViewport.Width - width) / 2;
        yPositionOnScreen = Math.Max(0, (Game1.uiViewport.Height - height) / 2);
        _body = new Rectangle(xPositionOnScreen + 24, yPositionOnScreen + 60, width - 48, height - 84);
        _maxScroll = Math.Max(0, contentHeight - _body.Height);
        _scroll = Math.Clamp(_scroll, 0, _maxScroll);
        _buttons.Clear();
        for (int i = 0; i < _choices.Length; i++)
            _buttons.Add(new Rectangle(_body.X, _body.Y + messageHeight + 20 + i * 52 - _scroll, _body.Width, 44));
    }

    public override void gameWindowSizeChanged(Rectangle oldBounds, Rectangle newBounds) => Layout();
    public override void receiveKeyPress(Keys key)
    {
        if (key == Keys.Escape) _back();
        else if (key == Keys.PageDown) receiveScrollWheelAction(-240);
        else if (key == Keys.PageUp) receiveScrollWheelAction(240);
    }
    public override void receiveScrollWheelAction(int direction) { _scroll -= direction / 4; Layout(); }
    public override void receiveLeftClick(int x, int y, bool playSound = true)
    {
        for (int i = 0; i < _buttons.Count; i++)
            if (_body.Contains(_buttons[i]) && _buttons[i].Contains(x, y)) { _choices[i].Click(); return; }
    }
    public override void draw(SpriteBatch b)
    {
        b.Draw(Game1.fadeToBlackRect, new Rectangle(0, 0, Game1.uiViewport.Width, Game1.uiViewport.Height), Color.Black * .45f);
        drawTextureBox(b, xPositionOnScreen, yPositionOnScreen, width, height, Color.White);
        Utility.drawTextWithShadow(b, _title, Game1.smallFont, new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 20), Game1.textColor);
        ClippedSpriteText.Draw(b, Game1.smallFont, _wrapped, new Vector2(_body.X, _body.Y - _scroll), Game1.textColor, _body);
        for (int i = 0; i < _buttons.Count; i++)
        {
            var r = _buttons[i];
            if (!_body.Contains(r)) continue;
            drawTextureBox(b, r.X, r.Y, r.Width, r.Height, r.Contains(Game1.getMouseX(true), Game1.getMouseY(true)) ? Color.Wheat : Color.White);
            b.DrawString(Game1.smallFont, _choices[i].Label, new Vector2(r.X + 12, r.Y + 8), Game1.textColor);
        }
        if (_maxScroll > 0)
        {
            var track = new Rectangle(_body.Right + 6, _body.Top, 4, _body.Height);
            b.Draw(Game1.fadeToBlackRect, track, Color.SaddleBrown * .2f);
            b.Draw(Game1.fadeToBlackRect, new Rectangle(track.X, track.Y + (track.Height - 24) * _scroll / _maxScroll, 4, 24), Color.SaddleBrown);
        }
        drawMouse(b);
    }
}
