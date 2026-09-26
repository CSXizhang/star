using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>A small single-utterance editor that returns to native companion dialogue.</summary>
public sealed class CompanionSpeechInputMenu : IClickableMenu
{
    private readonly TextBox _input;
    private bool _finished;
    private readonly Action<string> _send;
    private readonly Action _cancel;
    private readonly string _name;
    private readonly Rectangle _sendBounds;
    private readonly Rectangle _cancelBounds;

    public CompanionSpeechInputMenu(string name, Action<string> send, Action cancel)
        : base(Math.Max(16, (Game1.uiViewport.Width - 680) / 2),
               Math.Max(16, Game1.uiViewport.Height - 240),
               Math.Min(680, Game1.uiViewport.Width - 32), 192)
    {
        _name = name;
        _send = send;
        _cancel = cancel;
        _input = new TextBox(Game1.content.Load<Texture2D>(@"LooseSprites\textBox"), null, Game1.smallFont, Game1.textColor)
        {
            X = xPositionOnScreen + 24, Y = yPositionOnScreen + 64,
            Width = width - 48, Height = 44, textLimit = 500, limitWidth = true,
        };
        _sendBounds = new Rectangle(xPositionOnScreen + width - 184, yPositionOnScreen + 124, 72, 40);
        _cancelBounds = new Rectangle(xPositionOnScreen + width - 96, yPositionOnScreen + 124, 72, 40);
        _input.OnEnterPressed += _ => Finish(true);
        _input.SelectMe();
        Game1.keyboardDispatcher.Subscriber = _input;
    }

    private void Finish(bool send)
    {
        if (_finished) return;
        string text = _input.Text.Trim();
        if (send && text.Length == 0) return;
        _finished = true;
        exitThisMenu(false);
        if (send) _send(text); else _cancel();
    }

    protected override void cleanupBeforeExit()
    {
        _input.Selected = false;
        if (ReferenceEquals(Game1.keyboardDispatcher.Subscriber, _input))
            Game1.keyboardDispatcher.Subscriber = null;
        base.cleanupBeforeExit();
    }

    public override void receiveKeyPress(Keys key)
    {
        if (key == Keys.Enter) Finish(true);
        else if (key == Keys.Escape) Finish(false);
        else base.receiveKeyPress(key);
    }

    public override void receiveLeftClick(int x, int y, bool playSound = true)
    {
        if (_sendBounds.Contains(x, y)) Finish(true);
        else if (_cancelBounds.Contains(x, y)) Finish(false);
        else _input.Update();
    }

    public override void draw(SpriteBatch b)
    {
        drawTextureBox(b, xPositionOnScreen, yPositionOnScreen, width, height, Color.White);
        b.DrawString(Game1.smallFont, $"对{_name}说……", new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 24), Game1.textColor);
        _input.Draw(b);
        b.DrawString(Game1.smallFont, "说出", new Vector2(_sendBounds.X, _sendBounds.Y + 8), Game1.textColor);
        b.DrawString(Game1.smallFont, "返回", new Vector2(_cancelBounds.X, _cancelBounds.Y + 8), Game1.textColor);
        drawMouse(b);
    }
}
