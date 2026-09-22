using System;
using System.ComponentModel;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.Net.Http;
using System.Text;
using System.Web.Script.Serialization;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using AxITGExpertCtlLib;

namespace KisKospi200Bridge
{
    public sealed class MainForm : Form
    {
        private const string MarketAiBaseUrl = "http://127.0.0.1:8001";
        private const int ForwardIntervalSeconds = 5;
        private const int HeartbeatIntervalSeconds = 10;
        private const int RouteResolveIntervalSeconds = 5;
        private const int QuoteUniverseResolveIntervalSeconds = 10;
        private const string QuoteUniversePath = "/api/bridge/kis-efriend/quote-universe";
        private static readonly string[] SignalBaselineQuoteTickers = { "005930", "000660" };
        // Private messages owned by Investment Local Suite. The Bridge remains a
        // separate x86/ActiveX process, but its UI is exposed only through the
        // Local Suite tray icon.
        private const int LocalSuiteExitMessage = 0x8200;
        private const int LocalSuiteViewMessage = 0x8201;
        private const int WmSysCommand = 0x0112;
        private const int ScClose = 0xF060;

        // Visual tokens mirror the light Web Monitor so the tray-opened native
        // Bridge window and /monitor/ present the same operational surface.
        private static readonly Color UiBackground = Color.FromArgb(245, 247, 250);
        private static readonly Color UiSurface = Color.White;
        private static readonly Color UiSurfaceMuted = Color.FromArgb(248, 250, 252);
        private static readonly Color UiBorder = Color.FromArgb(220, 226, 232);
        private static readonly Color UiText = Color.FromArgb(24, 33, 43);
        private static readonly Color UiTextMuted = Color.FromArgb(105, 117, 134);
        private static readonly Color UiPositive = Color.FromArgb(217, 45, 32);
        private static readonly Color UiNegative = Color.FromArgb(21, 112, 239);
        private static readonly Color UiLive = Color.FromArgb(22, 128, 60);
        private static readonly Color UiLiveBg = Color.FromArgb(237, 249, 240);
        private static readonly Color UiClosed = Color.FromArgb(89, 101, 121);
        private static readonly Color UiClosedBg = Color.FromArgb(240, 242, 245);
        private static readonly Color UiWarming = Color.FromArgb(154, 103, 0);
        private static readonly Color UiWarmingBg = Color.FromArgb(255, 248, 223);
        private static readonly Color UiStale = Color.FromArgb(181, 71, 8);
        private static readonly Color UiStaleBg = Color.FromArgb(255, 244, 229);
        private static readonly Color UiError = Color.FromArgb(198, 40, 40);
        private static readonly Color UiErrorBg = Color.FromArgb(255, 240, 240);

        private readonly HttpClient httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
        private readonly System.Windows.Forms.Timer sessionTimer = new System.Windows.Forms.Timer();

        private AxITGExpertCtl axTrade;
        private AxITGExpertCtl axKospi;
        private readonly Dictionary<string, QuoteStreamRegistration> quoteStreams =
            new Dictionary<string, QuoteStreamRegistration>(StringComparer.Ordinal);
        private TextBox txtCode;
        private ComboBox cmbService;
        private Button btnStart;
        private Button btnStop;
        private Label lblStatus;
        private Label lblResolvedService;
        private Label lblTime;
        private Label lblPrice;
        private Label lblChangeRate;
        private TextBox txtLog;
        private Label lblSystemStatus;
        private Label lblClock;
        private Label lblSummaryTotal;
        private Label lblSummaryLive;
        private Label lblSummaryExtended;
        private Label lblSummaryClosed;
        private Label lblSummaryWarming;
        private Label lblSummaryStale;
        private Label lblSummaryError;
        private Label lblFooterStatus;
        private Label lblFooterLastReceived;
        private TableLayoutPanel holdingsGrid;
        private readonly Dictionary<string, HoldingQuoteCard> holdingCards =
            new Dictionary<string, HoldingQuoteCard>(StringComparer.Ordinal);
        private readonly HashSet<string> dashboardQuoteTickers =
            new HashSet<string>(StringComparer.Ordinal);
        private readonly List<string> dashboardQuoteDisplayOrder =
            new List<string>();
        private readonly List<string> holdingCardOrder =
            new List<string>();
        private readonly Dictionary<string, string> dashboardQuoteNames =
            new Dictionary<string, string>(StringComparer.Ordinal);
        private readonly Dictionary<string, string> dashboardMarketStates =
            new Dictionary<string, string>(StringComparer.Ordinal);
        private string cashMarketState = "unknown";
        private string lastTradeError = "";
        private bool tradeFreshTickRequired;
        private string lastTradeBusinessTime = "";
        private string lastTradePriceText = "";
        private string lastTradeRateText = "";
        private DateTime? lastTradeCachedObservedUtc;

        private string activeService = "";
        private string activeCode = "";
        private bool monitoringRequested;
        private long tickCount;
        private long forwardSuccessCount;
        private int forwardInFlight;
        private int heartbeatInFlight;
        private int routeResolveInFlight;
        private int quoteUniverseResolveInFlight;
        private DateTime lastForwardAttemptUtc = DateTime.MinValue;
        private DateTime lastHeartbeatAttemptUtc = DateTime.MinValue;
        private DateTime lastRouteResolveAttemptUtc = DateTime.MinValue;
        private DateTime lastQuoteUniverseResolveAttemptUtc = DateTime.MinValue;
        private DateTime? lastTickUtc;
        private string lastForwardError = "";
        private string autoResolvedCode = "";
        private string autoResolvedService = "";
        private string autoResolvedSession = "closed";
        private bool autoRouteResolved;
        private DateTime lastForwardErrorLogUtc = DateTime.MinValue;
        private DateTime lastQuoteUniverseErrorLogUtc = DateTime.MinValue;
        private string lastQuoteUniverseError = "";
        private bool quoteUniverseResolved;
        private bool exitRequested;

        private readonly SpotStreamState kospiSpot = new SpotStreamState(
            "KOSPI", "INDEX:KOSPI", "JUC_R", "0001", 1, 2, -1, 9, 5, -1, -1);

        public MainForm()
        {
            InitializeComponent();
            cmbService.SelectedIndex = 0;
            sessionTimer.Interval = 5000;
            sessionTimer.Tick += SessionTimer_Tick;
            sessionTimer.Start();
            AppendLog("Bridge ready. eFriend Expert 로그인 상태에서 실시간 수신이 자동 시작됩니다.");
        }

        private void InitializeComponent()
        {
            var resources = new ComponentResourceManager(typeof(MainForm));
            axTrade = new AxITGExpertCtl();
            axKospi = new AxITGExpertCtl();
            txtCode = new TextBox();
            cmbService = new ComboBox();
            btnStart = new Button();
            btnStop = new Button();

            // Debug logging remains internal; the monitor surface only shows
            // operational status needed for one-glance verification.
            txtLog = new TextBox
            {
                Multiline = true,
                ReadOnly = true,
                Visible = false
            };

            ((ISupportInitialize)axTrade).BeginInit();
            ((ISupportInitialize)axKospi).BeginInit();
            SuspendLayout();

            Text = "KIS eFriend Market Bridge - Realtime Monitor";
            StartPosition = FormStartPosition.CenterScreen;
            ShowInTaskbar = false;
            Opacity = 0;
            ClientSize = new Size(1180, 790);
            MinimumSize = new Size(1040, 700);
            Font = UiFont(9.5F);
            ForeColor = UiText;
            BackColor = UiBackground;

            // Automatic K200 route/subscription controls are kept off-screen. The
            // bridge starts in AUTO and is operated from Investment Local Suite.
            txtCode.Text = "AUTO";
            txtCode.CharacterCasing = CharacterCasing.Upper;
            cmbService.DropDownStyle = ComboBoxStyle.DropDownList;
            cmbService.Items.AddRange(new object[] { "AUTO", "FC_R (주간)", "CMEC_R (야간)" });
            btnStart.Click += BtnStart_Click;
            btnStop.Click += BtnStop_Click;

            var root = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                Padding = new Padding(16),
                ColumnCount = 1,
                RowCount = 4,
                BackColor = UiBackground
            };
            root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 56F));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 202F));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 42F));

            var header = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 2,
                RowCount = 1,
                Margin = new Padding(4, 0, 4, 12),
                BackColor = UiBackground
            };
            header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            header.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            header.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));

            var title = new Label
            {
                Dock = DockStyle.Fill,
                Text = "보유종목 실시간 시세",
                Font = UiFont(12F, true),
                ForeColor = UiText,
                TextAlign = ContentAlignment.MiddleLeft,
                Margin = new Padding(0)
            };
            header.Controls.Add(title, 0, 0);

            var headerRight = new FlowLayoutPanel
            {
                AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink,
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = false,
                Anchor = AnchorStyles.Right,
                Margin = new Padding(0)
            };
            lblSystemStatus = NewSystemStateLabel("● 연결 대기");
            lblSystemStatus.Margin = new Padding(0, 9, 12, 0);
            lblClock = NewPlainValueLabel("--:--:--", UiTextMuted, 9F);
            lblClock.AutoSize = true;
            lblClock.TextAlign = ContentAlignment.MiddleRight;
            lblClock.Margin = new Padding(0, 8, 0, 0);
            headerRight.Controls.Add(lblSystemStatus);
            headerRight.Controls.Add(lblClock);
            header.Controls.Add(headerRight, 1, 0);
            root.Controls.Add(header, 0, 0);

            var marketCards = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 2,
                RowCount = 1,
                Margin = new Padding(0, 0, 0, 12),
                BackColor = UiBackground
            };
            marketCards.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            marketCards.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            marketCards.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));

            var k200Card = CreateMarketCard("K200", "KOSPI200 선물", "-");
            k200Card.Container.Margin = new Padding(0, 0, 6, 0);
            lblStatus = k200Card.Status;
            lblResolvedService = k200Card.Service;
            lblTime = k200Card.Time;
            lblPrice = k200Card.Price;
            lblChangeRate = k200Card.Rate;
            marketCards.Controls.Add(k200Card.Container, 0, 0);

            var kospiCard = CreateMarketCard("KOSPI", "코스피 현물", "정규장");
            kospiCard.Container.Margin = new Padding(6, 0, 0, 0);
            kospiSpot.UiStatus = kospiCard.Status;
            kospiSpot.UiService = kospiCard.Service;
            kospiSpot.UiTime = kospiCard.Time;
            kospiSpot.UiPrice = kospiCard.Price;
            kospiSpot.UiRate = kospiCard.Rate;
            marketCards.Controls.Add(kospiCard.Container, 1, 0);
            root.Controls.Add(marketCards, 0, 1);

            var holdingsSection = new RoundedPanel
            {
                Dock = DockStyle.Fill,
                Radius = 12,
                BorderColor = UiBorder,
                FillColor = UiSurface,
                Margin = new Padding(0, 0, 0, 12),
                Padding = new Padding(1)
            };

            var summaryHeader = new Panel
            {
                Dock = DockStyle.Top,
                Height = 48,
                BackColor = UiSurfaceMuted,
                Padding = new Padding(16, 0, 16, 0)
            };

            var holdingsTitle = new Label
            {
                AutoSize = true,
                Text = "보유종목",
                Font = UiFont(9.75F, true),
                ForeColor = UiText,
                Location = new Point(16, 15)
            };
            summaryHeader.Controls.Add(holdingsTitle);

            var summaryStats = new FlowLayoutPanel
            {
                AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink,
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = false,
                Anchor = AnchorStyles.Top | AnchorStyles.Right,
                BackColor = UiSurfaceMuted,
                Margin = new Padding(0),
                Padding = new Padding(0)
            };
            summaryStats.Controls.Add(CreateSummaryItem("전체", UiText, out lblSummaryTotal));
            summaryStats.Controls.Add(CreateSummaryItem("정상", UiLive, out lblSummaryLive));
            summaryStats.Controls.Add(CreateSummaryItem("시간외", UiLive, out lblSummaryExtended));
            summaryStats.Controls.Add(CreateSummaryItem("장마감", UiClosed, out lblSummaryClosed));
            summaryStats.Controls.Add(CreateSummaryItem("대기", UiWarming, out lblSummaryWarming));
            summaryStats.Controls.Add(CreateSummaryItem("지연", UiStale, out lblSummaryStale));
            summaryStats.Controls.Add(CreateSummaryItem("오류", UiError, out lblSummaryError));
            summaryHeader.Controls.Add(summaryStats);
            summaryHeader.Resize += (sender, args) =>
            {
                summaryStats.Location = new Point(
                    Math.Max(holdingsTitle.Right + 24, summaryHeader.ClientSize.Width - summaryStats.Width - 16),
                    13);
            };

            var summaryBorder = new Panel
            {
                Dock = DockStyle.Bottom,
                Height = 1,
                BackColor = UiBorder
            };
            summaryHeader.Controls.Add(summaryBorder);

            holdingsGrid = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                AutoScroll = true,
                ColumnCount = 5,
                RowCount = 1,
                Margin = new Padding(0),
                Padding = new Padding(6),
                GrowStyle = TableLayoutPanelGrowStyle.AddRows,
                BackColor = UiSurface
            };
            for (var i = 0; i < 5; i++)
                holdingsGrid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 20F));
            holdingsGrid.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));

            var holdingsLayout = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 1,
                RowCount = 2,
                BackColor = UiSurface,
                Margin = new Padding(0),
                Padding = new Padding(0)
            };
            holdingsLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            holdingsLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 48F));
            holdingsLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            summaryHeader.Dock = DockStyle.Fill;
            holdingsLayout.Controls.Add(summaryHeader, 0, 0);
            holdingsLayout.Controls.Add(holdingsGrid, 0, 1);
            holdingsSection.Controls.Add(holdingsLayout);
            root.Controls.Add(holdingsSection, 0, 2);

            var footer = new RoundedPanel
            {
                Dock = DockStyle.Fill,
                Radius = 8,
                BorderColor = UiBorder,
                FillColor = UiSurface,
                Margin = new Padding(0),
                Padding = new Padding(16, 0, 16, 0)
            };
            var footerLayout = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 2,
                RowCount = 1,
                BackColor = Color.Transparent,
                Margin = new Padding(0),
                Padding = new Padding(0)
            };
            footerLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            footerLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            footerLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));

            lblFooterStatus = NewSystemStateLabel("● 연결: 확인 중");
            lblFooterStatus.Dock = DockStyle.Fill;
            lblFooterStatus.TextAlign = ContentAlignment.MiddleLeft;
            lblFooterLastReceived = NewPlainValueLabel("마지막 수신: --:--:--", UiTextMuted, 8.25F);
            lblFooterLastReceived.Dock = DockStyle.Fill;
            lblFooterLastReceived.TextAlign = ContentAlignment.MiddleRight;
            footerLayout.Controls.Add(lblFooterStatus, 0, 0);
            footerLayout.Controls.Add(lblFooterLastReceived, 1, 0);
            footer.Controls.Add(footerLayout);
            root.Controls.Add(footer, 0, 3);

            axTrade.Enabled = true;
            axTrade.Location = new Point(0, 0);
            axTrade.Name = "axTrade";
            axTrade.OcxState = (AxHost.State)resources.GetObject("axTrade.OcxState");
            axTrade.Size = new Size(12, 12);
            axTrade.TabIndex = 99;
            axTrade.ReceiveRealData += AxTrade_ReceiveRealData;
            Controls.Add(axTrade);

            ConfigureSpotControl(axKospi, resources, "axKospi", new Point(0, 0));
            axKospi.ReceiveRealData += AxKospi_ReceiveRealData;
            Controls.Add(axKospi);

            Controls.Add(root);
            root.BringToFront();

            var appIcon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath) ?? SystemIcons.Application;
            Icon = appIcon;

            Shown += MainForm_Shown;
            Resize += MainForm_Resize;
            FormClosing += MainForm_FormClosing;

            ((ISupportInitialize)axTrade).EndInit();
            ((ISupportInitialize)axKospi).EndInit();
            ResumeLayout(false);
            PerformLayout();
            UpdateMonitorUi();
        }

        private static void ConfigureSpotControl(
            AxITGExpertCtl control,
            ComponentResourceManager resources,
            string name,
            Point location)
        {
            control.Enabled = true;
            control.Location = location;
            control.Name = name;
            control.OcxState = (AxHost.State)resources.GetObject("axTrade.OcxState");
            control.Size = new Size(12, 12);
            control.TabIndex = 99;
        }

        private static Font UiFont(float size, bool bold = false)
        {
            return new Font(
                "Malgun Gothic",
                size,
                bold ? FontStyle.Bold : FontStyle.Regular,
                GraphicsUnit.Point,
                129);
        }

        private static Label NewPlainValueLabel(
            string text,
            Color? color = null,
            float size = 9.5F,
            bool bold = false)
        {
            return new Label
            {
                AutoSize = false,
                Text = text,
                Font = UiFont(size, bold),
                ForeColor = color ?? UiText,
                TextAlign = ContentAlignment.MiddleLeft,
                BackColor = Color.Transparent,
                Margin = new Padding(0)
            };
        }

        private static Label NewSystemStateLabel(string text)
        {
            return new Label
            {
                AutoSize = true,
                Text = text,
                Font = UiFont(8.75F),
                ForeColor = UiWarming,
                BackColor = Color.Transparent,
                TextAlign = ContentAlignment.MiddleLeft,
                Margin = new Padding(0)
            };
        }

        private static PillLabel NewStatusBadge(string text)
        {
            return new PillLabel
            {
                AutoSize = false,
                Size = new Size(64, 24),
                MinimumSize = new Size(48, 24),
                Text = text,
                Font = UiFont(8.25F, true),
                TextAlign = ContentAlignment.MiddleCenter,
                ForeColor = UiWarming,
                FillColor = UiWarmingBg,
                Radius = 12,
                Margin = new Padding(0)
            };
        }

        private static Control CreateSummaryItem(string title, Color valueColor, out Label valueLabel)
        {
            var host = new FlowLayoutPanel
            {
                AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink,
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = false,
                BackColor = UiSurfaceMuted,
                Margin = new Padding(0, 0, 16, 0),
                Padding = new Padding(0)
            };

            var titleLabel = new Label
            {
                AutoSize = true,
                Text = title,
                Font = UiFont(8.25F),
                ForeColor = UiTextMuted,
                BackColor = Color.Transparent,
                Margin = new Padding(0, 1, 4, 0)
            };
            valueLabel = new Label
            {
                AutoSize = true,
                Text = "0",
                Font = UiFont(8.25F, true),
                ForeColor = valueColor,
                BackColor = Color.Transparent,
                Margin = new Padding(0)
            };

            host.Controls.Add(titleLabel);
            host.Controls.Add(valueLabel);
            return host;
        }

        private static MarketCardUi CreateMarketCard(string title, string subtitle, string serviceText)
        {
            var card = new RoundedPanel
            {
                Dock = DockStyle.Fill,
                Radius = 12,
                BorderColor = UiBorder,
                FillColor = UiSurface,
                Padding = new Padding(16)
            };

            var layout = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 1,
                RowCount = 3,
                BackColor = Color.Transparent,
                Margin = new Padding(0),
                Padding = new Padding(0)
            };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 46F));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 44F));

            var header = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 2,
                RowCount = 1,
                BackColor = Color.Transparent,
                Margin = new Padding(0)
            };
            header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            header.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            var identity = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 1,
                RowCount = 2,
                BackColor = Color.Transparent,
                Margin = new Padding(0)
            };
            identity.RowStyles.Add(new RowStyle(SizeType.Absolute, 23F));
            identity.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            var titleLabel = NewPlainValueLabel(title, UiText, 12F, true);
            titleLabel.Dock = DockStyle.Fill;
            var subtitleLabel = NewPlainValueLabel(subtitle, UiTextMuted, 8.25F);
            subtitleLabel.Dock = DockStyle.Fill;
            subtitleLabel.TextAlign = ContentAlignment.TopLeft;
            identity.Controls.Add(titleLabel, 0, 0);
            identity.Controls.Add(subtitleLabel, 0, 1);

            var status = NewStatusBadge("대기");
            status.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            status.Margin = new Padding(0, 0, 0, 0);

            header.Controls.Add(identity, 0, 0);
            header.Controls.Add(status, 1, 0);

            var valueFlow = new FlowLayoutPanel
            {
                Dock = DockStyle.Fill,
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = false,
                BackColor = Color.Transparent,
                Padding = new Padding(0, 8, 0, 0),
                Margin = new Padding(0)
            };
            var price = new Label
            {
                AutoSize = true,
                Text = "-",
                Font = UiFont(22.5F, true),
                ForeColor = UiText,
                BackColor = Color.Transparent,
                Margin = new Padding(0, 0, 12, 0)
            };
            var rate = new Label
            {
                AutoSize = true,
                Text = "-",
                Font = UiFont(9.5F, true),
                ForeColor = UiTextMuted,
                BackColor = Color.Transparent,
                Margin = new Padding(0, 16, 0, 0)
            };
            valueFlow.Controls.Add(price);
            valueFlow.Controls.Add(rate);

            var metrics = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 2,
                RowCount = 2,
                BackColor = Color.Transparent,
                Margin = new Padding(0)
            };
            metrics.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 35F));
            metrics.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 65F));
            metrics.RowStyles.Add(new RowStyle(SizeType.Percent, 50F));
            metrics.RowStyles.Add(new RowStyle(SizeType.Percent, 50F));

            var serviceTitle = NewPlainValueLabel("세션", UiTextMuted, 8.25F);
            serviceTitle.Dock = DockStyle.Fill;
            var timeTitle = NewPlainValueLabel("시간", UiTextMuted, 8.25F);
            timeTitle.Dock = DockStyle.Fill;
            var service = NewPlainValueLabel(serviceText, UiText, 8.25F);
            service.Dock = DockStyle.Fill;
            service.TextAlign = ContentAlignment.MiddleRight;
            var time = NewPlainValueLabel("--:--:--", UiText, 8.25F);
            time.Dock = DockStyle.Fill;
            time.TextAlign = ContentAlignment.MiddleRight;

            metrics.Controls.Add(serviceTitle, 0, 0);
            metrics.Controls.Add(service, 1, 0);
            metrics.Controls.Add(timeTitle, 0, 1);
            metrics.Controls.Add(time, 1, 1);

            layout.Controls.Add(header, 0, 0);
            layout.Controls.Add(valueFlow, 0, 1);
            layout.Controls.Add(metrics, 0, 2);
            card.Controls.Add(layout);

            return new MarketCardUi(card, status, service, time, price, rate);
        }

        private HoldingQuoteCard CreateHoldingCard(string ticker)
        {
            var card = new RoundedPanel
            {
                Dock = DockStyle.Fill,
                Radius = 12,
                BorderColor = UiBorder,
                FillColor = UiSurface,
                Padding = new Padding(12),
                Margin = new Padding(6)
            };

            var layout = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 1,
                RowCount = 3,
                BackColor = Color.Transparent,
                Margin = new Padding(0),
                Padding = new Padding(0)
            };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 44F));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 22F));

            var header = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 2,
                RowCount = 1,
                BackColor = Color.Transparent,
                Margin = new Padding(0)
            };
            header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            header.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            var identity = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 1,
                RowCount = 2,
                BackColor = Color.Transparent,
                Margin = new Padding(0)
            };
            identity.RowStyles.Add(new RowStyle(SizeType.Absolute, 23F));
            identity.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));

            var nameLabel = NewPlainValueLabel(QuoteDisplayName(ticker), UiText, 9.75F, true);
            nameLabel.Dock = DockStyle.Fill;
            nameLabel.AutoEllipsis = true;
            var tickerLabel = NewPlainValueLabel(ticker, UiTextMuted, 8.25F);
            tickerLabel.Dock = DockStyle.Fill;
            tickerLabel.TextAlign = ContentAlignment.TopLeft;
            identity.Controls.Add(nameLabel, 0, 0);
            identity.Controls.Add(tickerLabel, 0, 1);

            var status = NewStatusBadge("대기");
            status.Size = new Size(58, 24);
            status.Anchor = AnchorStyles.Top | AnchorStyles.Right;

            header.Controls.Add(identity, 0, 0);
            header.Controls.Add(status, 1, 0);

            var value = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 1,
                RowCount = 2,
                BackColor = Color.Transparent,
                Margin = new Padding(0),
                Padding = new Padding(0, 8, 0, 0)
            };
            value.RowStyles.Add(new RowStyle(SizeType.Percent, 62F));
            value.RowStyles.Add(new RowStyle(SizeType.Percent, 38F));
            var price = NewPlainValueLabel("-", UiText, 18F, true);
            price.Dock = DockStyle.Fill;
            price.AutoEllipsis = true;
            price.TextAlign = ContentAlignment.BottomLeft;
            var rate = NewPlainValueLabel("-", UiTextMuted, 9.25F, true);
            rate.Dock = DockStyle.Fill;
            rate.TextAlign = ContentAlignment.TopLeft;
            value.Controls.Add(price, 0, 0);
            value.Controls.Add(rate, 0, 1);

            var time = NewPlainValueLabel("--:--:--", UiTextMuted, 8.25F);
            time.Dock = DockStyle.Fill;
            time.TextAlign = ContentAlignment.BottomLeft;

            layout.Controls.Add(header, 0, 0);
            layout.Controls.Add(value, 0, 1);
            layout.Controls.Add(time, 0, 2);
            card.Controls.Add(layout);

            return new HoldingQuoteCard(ticker, card, nameLabel, status, time, price, rate);
        }

        private List<string> DashboardDisplayTickers()
        {
            var result = new List<string>();
            var seen = new HashSet<string>(StringComparer.Ordinal);

            foreach (var ticker in dashboardQuoteDisplayOrder)
            {
                if (dashboardQuoteTickers.Contains(ticker) && seen.Add(ticker))
                    result.Add(ticker);
            }

            var fallback = new List<string>(dashboardQuoteTickers);
            fallback.Sort(StringComparer.Ordinal);
            foreach (var ticker in fallback)
            {
                if (seen.Add(ticker))
                    result.Add(ticker);
            }
            return result;
        }

        private void RebuildHoldingCards()
        {
            if (holdingsGrid == null) return;

            var ordered = DashboardDisplayTickers();
            var same = ordered.Count == holdingCardOrder.Count;
            if (same)
            {
                for (var index = 0; index < ordered.Count; index++)
                {
                    if (!string.Equals(ordered[index], holdingCardOrder[index], StringComparison.Ordinal))
                    {
                        same = false;
                        break;
                    }
                }
            }
            if (same) return;

            holdingsGrid.SuspendLayout();
            holdingsGrid.Controls.Clear();
            holdingsGrid.RowStyles.Clear();
            holdingCards.Clear();
            holdingCardOrder.Clear();

            var rows = Math.Max(1, (ordered.Count + 4) / 5);
            holdingsGrid.RowCount = rows;
            for (var row = 0; row < rows; row++)
                holdingsGrid.RowStyles.Add(new RowStyle(SizeType.Percent, 100F / rows));

            for (var index = 0; index < ordered.Count; index++)
            {
                var ticker = ordered[index];
                var card = CreateHoldingCard(ticker);
                holdingCards[ticker] = card;
                holdingCardOrder.Add(ticker);
                holdingsGrid.Controls.Add(card.Container, index % 5, index / 5);
            }

            holdingsGrid.ResumeLayout();
        }

        private void UpdateMonitorUi()
        {
            if (IsDisposed || Disposing) return;

            if (lblClock != null)
                lblClock.Text = DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture);

            if (lblStatus != null)
                SetStatusBadge(lblStatus, ResolveK200UiState());
            if (lblResolvedService != null)
                lblResolvedService.Text = ResolveK200SessionLabel();
            if (lblTime != null)
                lblTime.Text = string.IsNullOrWhiteSpace(lastTradeBusinessTime) ? "--:--:--" : FormatBusinessTime(lastTradeBusinessTime);
            if (lblPrice != null)
                lblPrice.Text = string.IsNullOrWhiteSpace(lastTradePriceText) ? "-" : FormatNumber(lastTradePriceText, 2);
            if (lblChangeRate != null)
            {
                lblChangeRate.Text = string.IsNullOrWhiteSpace(lastTradeRateText) ? "-" : FormatSignedPercent(lastTradeRateText);
                ApplyRateColor(lblChangeRate, lastTradeRateText);
            }

            if (kospiSpot.UiStatus != null)
                SetStatusBadge(kospiSpot.UiStatus, ResolveSpotUiState(kospiSpot));

            RebuildHoldingCards();

            var live = 0;
            var extended = 0;
            var closed = 0;
            var warming = 0;
            var stale = 0;
            var error = 0;

            foreach (var ticker in DashboardDisplayTickers())
            {
                QuoteStreamRegistration registration;
                var stateText = "대기";
                if (quoteStreams.TryGetValue(ticker, out registration))
                    stateText = ResolveSpotUiState(registration.State);

                if (stateText == "정상") live++;
                else if (stateText == "시간외") extended++;
                else if (stateText == "장마감") closed++;
                else if (stateText == "지연") stale++;
                else if (stateText == "오류") error++;
                else warming++;

                HoldingQuoteCard card;
                if (!holdingCards.TryGetValue(ticker, out card)) continue;

                if (card.NameLabel != null)
                    card.NameLabel.Text = QuoteDisplayName(ticker);
                SetStatusBadge(card.Status, stateText);
                if (registration == null)
                {
                    card.Time.Text = "--:--:--";
                    card.Price.Text = "-";
                    card.Rate.Text = "-";
                    card.Rate.ForeColor = UiTextMuted;
                    continue;
                }
                var state = registration.State;
                card.Time.Text = string.IsNullOrWhiteSpace(state.LastBusinessTime) ? "--:--:--" : FormatBusinessTime(state.LastBusinessTime);
                card.Price.Text = string.IsNullOrWhiteSpace(state.LastPriceText) ? "-" : FormatSpotPrice(state, state.LastPriceText);
                card.Rate.Text = FormatHoldingChange(state.LastRateText, state.LastChangeAmountText);
                ApplyRateColor(card.Rate, state.LastRateText);
            }

            var total = dashboardQuoteTickers.Count;
            if (lblSummaryTotal != null) lblSummaryTotal.Text = total.ToString(CultureInfo.InvariantCulture);
            if (lblSummaryLive != null) lblSummaryLive.Text = live.ToString(CultureInfo.InvariantCulture);
            if (lblSummaryExtended != null) lblSummaryExtended.Text = extended.ToString(CultureInfo.InvariantCulture);
            if (lblSummaryClosed != null) lblSummaryClosed.Text = closed.ToString(CultureInfo.InvariantCulture);
            if (lblSummaryWarming != null) lblSummaryWarming.Text = warming.ToString(CultureInfo.InvariantCulture);
            if (lblSummaryStale != null) lblSummaryStale.Text = stale.ToString(CultureInfo.InvariantCulture);
            if (lblSummaryError != null) lblSummaryError.Text = error.ToString(CultureInfo.InvariantCulture);

            var connection = ResolveSystemConnectionText(error);
            if (lblSystemStatus != null)
                SetSystemState(lblSystemStatus, connection, "시스템 ");

            if (lblFooterStatus != null)
                SetSystemState(lblFooterStatus, connection, "연결: ");
            if (lblFooterLastReceived != null)
                lblFooterLastReceived.Text = "마지막 수신: " + LatestReceivedText();
        }

        private string ResolveSystemConnectionText(int holdingsErrorCount)
        {
            if (!monitoringRequested) return "중지";
            if (!string.IsNullOrEmpty(lastForwardError)) return "오류";
            if (!quoteUniverseResolved && !string.IsNullOrEmpty(lastQuoteUniverseError)) return "오류";
            if (!string.IsNullOrEmpty(lastTradeError) || !string.IsNullOrEmpty(kospiSpot.LastError))
                return "확인 필요";
            if (holdingsErrorCount > 0) return "확인 필요";
            foreach (var registration in quoteStreams.Values)
            {
                if (!string.IsNullOrEmpty(registration.State.LastError))
                    return "확인 필요";
            }
            if (!quoteUniverseResolved && forwardSuccessCount == 0 && kospiSpot.ForwardSuccessCount == 0) return "연결 중";
            return "정상";
        }

        private static string SessionLabelForService(string service)
        {
            if (string.Equals(service, "FC_R", StringComparison.Ordinal)) return "주간";
            if (string.Equals(service, "CMEC_R", StringComparison.Ordinal)) return "야간";
            return "-";
        }

        private string ResolveK200SessionLabel()
        {
            if (autoRouteResolved && string.Equals(autoResolvedSession, "closed", StringComparison.Ordinal))
                return "장마감";
            if (!string.IsNullOrWhiteSpace(activeService))
                return SessionLabelForService(activeService);
            if (autoRouteResolved)
            {
                if (string.Equals(autoResolvedSession, "day", StringComparison.Ordinal)) return "주간";
                if (string.Equals(autoResolvedSession, "night", StringComparison.Ordinal)) return "야간";
            }
            return "-";
        }

        private string ResolveK200UiState()
        {
            if (!string.IsNullOrEmpty(lastTradeError)) return "오류";
            if (autoRouteResolved && string.Equals(autoResolvedSession, "closed", StringComparison.Ordinal))
                return "장마감";
            if (!monitoringRequested) return "대기";
            if (tradeFreshTickRequired) return "대기";
            if (!lastTickUtc.HasValue) return "대기";
            if (ToKst(lastTickUtc.Value).Date != ToKst(DateTime.UtcNow).Date) return "지연";
            return "정상";
        }

        private string ResolveSpotUiState(SpotStreamState state)
        {
            if (state == null) return "대기";
            if (!string.IsNullOrEmpty(state.LastError)) return "오류";
            if (!monitoringRequested) return "대기";
            if (!state.Subscribed) return "오류";
            var marketState = state.Symbol.StartsWith("KRX:", StringComparison.Ordinal)
                ? state.MarketState
                : cashMarketState;
            if (!state.LastTickUtc.HasValue || state.TickCount <= 0)
            {
                if (string.Equals(marketState, "closed", StringComparison.OrdinalIgnoreCase) &&
                    state.CachedObservedUtc.HasValue)
                    return "장마감";
                if (state.CachedObservedUtc.HasValue)
                    return "지연";
                return "대기";
            }
            if (state.FreshTickRequired) return "지연";
            // A verified closing tick remains the latest completed-session value
            // across midnight; only an active/extended session requires today's tick.
            if (string.Equals(marketState, "closed", StringComparison.OrdinalIgnoreCase)) return "장마감";
            if (ToKst(state.LastTickUtc.Value).Date != ToKst(DateTime.UtcNow).Date) return "지연";
            if (string.Equals(marketState, "extended", StringComparison.OrdinalIgnoreCase)) return "시간외";
            return "정상";
        }

        private static DateTime ToKst(DateTime utc)
        {
            try
            {
                var zone = TimeZoneInfo.FindSystemTimeZoneById("Korea Standard Time");
                return TimeZoneInfo.ConvertTimeFromUtc(
                    utc.Kind == DateTimeKind.Utc ? utc : utc.ToUniversalTime(),
                    zone);
            }
            catch
            {
                return utc.ToLocalTime();
            }
        }

        private string LatestReceivedText()
        {
            DateTime? latest = lastTickUtc;
            if (lastTradeCachedObservedUtc.HasValue && (!latest.HasValue || lastTradeCachedObservedUtc.Value > latest.Value))
                latest = lastTradeCachedObservedUtc;
            if (kospiSpot.LastTickUtc.HasValue && (!latest.HasValue || kospiSpot.LastTickUtc.Value > latest.Value))
                latest = kospiSpot.LastTickUtc;
            if (kospiSpot.CachedObservedUtc.HasValue && (!latest.HasValue || kospiSpot.CachedObservedUtc.Value > latest.Value))
                latest = kospiSpot.CachedObservedUtc;
            foreach (var ticker in dashboardQuoteTickers)
            {
                QuoteStreamRegistration registration;
                if (!quoteStreams.TryGetValue(ticker, out registration)) continue;
                var value = registration.State.LastTickUtc;
                if (value.HasValue && (!latest.HasValue || value.Value > latest.Value))
                    latest = value;
                var cached = registration.State.CachedObservedUtc;
                if (cached.HasValue && (!latest.HasValue || cached.Value > latest.Value))
                    latest = cached;
            }
            return latest.HasValue ? ToKst(latest.Value).ToString("HH:mm:ss", CultureInfo.InvariantCulture) : "-";
        }

        private static void ResolveStatusColors(string state, out Color foreground, out Color background)
        {
            foreground = UiWarming;
            background = UiWarmingBg;

            if (state == "정상" || state == "시간외")
            {
                foreground = UiLive;
                background = UiLiveBg;
            }
            else if (state == "장마감" || state == "중지")
            {
                foreground = UiClosed;
                background = UiClosedBg;
            }
            else if (state == "지연" || state == "확인 필요")
            {
                foreground = UiStale;
                background = UiStaleBg;
            }
            else if (state == "오류")
            {
                foreground = UiError;
                background = UiErrorBg;
            }
        }

        private static void SetStatusBadge(Label label, string state)
        {
            if (label == null) return;

            Color foreground;
            Color background;
            ResolveStatusColors(state, out foreground, out background);

            label.Text = state;
            label.ForeColor = foreground;
            var pill = label as PillLabel;
            if (pill != null)
                pill.FillColor = background;
            else
                label.BackColor = background;
        }

        private static void SetSystemState(Label label, string state, string prefix)
        {
            if (label == null) return;

            Color foreground;
            Color background;
            ResolveStatusColors(state, out foreground, out background);
            label.ForeColor = foreground;
            label.Text = "● " + prefix + state;
        }

        private static void ApplyRateColor(Label label, string rawValue)
        {
            if (label == null) return;
            double value;
            if (!double.TryParse(rawValue, NumberStyles.Any, CultureInfo.InvariantCulture, out value))
            {
                label.ForeColor = UiTextMuted;
                return;
            }
            if (value > 0) label.ForeColor = UiPositive;
            else if (value < 0) label.ForeColor = UiNegative;
            else label.ForeColor = UiTextMuted;
        }

        private static void UpdateSpotUi(SpotStreamState state, string businessTime, string price, string rate, string volume, string ask, string bid)
        {
            if (state.UiService != null) state.UiService.Text = state.Symbol == "INDEX:KOSPI" ? "정규장" : "-";
            if (state.UiTime != null) state.UiTime.Text = FormatBusinessTime(businessTime);
            if (state.UiPrice != null) state.UiPrice.Text = FormatSpotPrice(state, price);
            if (state.UiRate != null)
            {
                state.UiRate.Text = FormatSignedPercent(rate);
                ApplyRateColor(state.UiRate, rate);
            }
        }

        private static string FormatSpotPrice(SpotStreamState state, string value)
        {
            return FormatNumber(value, state.Symbol == "INDEX:KOSPI" ? 2 : 0);
        }

        private async void MainForm_Shown(object sender, EventArgs e)
        {
            // Let WinForms/ActiveX finish creating the control, but keep the Bridge
            // invisible on normal startup. Investment Local Suite owns the only
            // system-tray icon and can reopen this window via a private message.
            HideBridgeWindow();
            Opacity = 1;

            await Task.Delay(800);
            if (IsDisposed || Disposing || monitoringRequested || !btnStart.Enabled) return;
            AppendLog("AUTO START - 실시간 수신을 자동으로 시작합니다.");
            StartMonitoring(false);
        }


        private void RequestApplicationExit()
        {
            if (IsDisposed || Disposing) return;
            exitRequested = true;
            Close();
        }

        protected override void WndProc(ref Message m)
        {
            // Intercept the title-bar X / Alt+F4 before WinForms starts a real
            // close transaction. Cancelling FormClosing after Close() has already
            // begun can produce a visible close/cancel flicker. The Bridge is a
            // resident helper process, so user-close means an immediate hide.
            if (m.Msg == WmSysCommand && (((int)m.WParam) & 0xFFF0) == ScClose && !exitRequested)
            {
                HideBridgeWindow();
                m.Result = IntPtr.Zero;
                return;
            }

            if (m.Msg == LocalSuiteExitMessage)
            {
                if (IsHandleCreated && !IsDisposed && !Disposing)
                    BeginInvoke(new Action(RequestApplicationExit));
                m.Result = IntPtr.Zero;
                return;
            }
            if (m.Msg == LocalSuiteViewMessage)
            {
                if (IsHandleCreated && !IsDisposed && !Disposing)
                    BeginInvoke(new Action(ShowBridgeWindow));
                m.Result = IntPtr.Zero;
                return;
            }
            base.WndProc(ref m);
        }

        private void MainForm_Resize(object sender, EventArgs e)
        {
            if (WindowState == FormWindowState.Minimized)
                HideBridgeWindow();
        }

        private void ShowBridgeWindow()
        {
            if (IsDisposed || Disposing) return;

            if (WindowState == FormWindowState.Minimized)
                WindowState = FormWindowState.Normal;

            ShowInTaskbar = true;
            if (!Visible)
                Show();
            Activate();
            BringToFront();
        }

        private void HideBridgeWindow()
        {
            if (IsDisposed || Disposing) return;
            // Hide first. Changing ShowInTaskbar while a visible WinForms window
            // is on screen may recreate its native handle and cause a brief flash.
            if (Visible)
                Hide();
            ShowInTaskbar = false;
        }

        private void BtnStart_Click(object sender, EventArgs e)
        {
            StartMonitoring(true);
        }

        private void StartMonitoring(bool showValidationMessage)
        {
            if (monitoringRequested) return;

            var code = (txtCode.Text ?? "").Trim().ToUpperInvariant();
            if (code.Length == 0)
            {
                if (showValidationMessage)
                    MessageBox.Show("AUTO 또는 선물 종목코드를 입력해 주세요.", "KIS Bridge", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                else
                    AppendLog("AUTO START SKIP - 종목코드가 비어 있습니다.");
                return;
            }

            monitoringRequested = true;
            activeCode = "";
            tickCount = 0;
            forwardSuccessCount = 0;
            btnStart.Enabled = false;
            btnStop.Enabled = true;
            txtCode.Enabled = false;
            cmbService.Enabled = false;

            EnsureBaselineQuoteStreams();
            EnsureSpotSubscriptions();
            UpdateDomesticStatus();
            _ = RefreshQuoteUniverseAsync(true);

            autoRouteResolved = false;
            if (NeedsAutoRoute())
                _ = RefreshAutoRouteAsync(true);
            else
            {
                EnsureSubscription();
                _ = SendHeartbeatAsync();
            }
        }

        private void BtnStop_Click(object sender, EventArgs e)
        {
            monitoringRequested = false;
            StopSubscription(true, true);
            StopSpotSubscriptions(true);
            UpdateDomesticStatus();
        }

        private void SessionTimer_Tick(object sender, EventArgs e)
        {
            UpdateMonitorUi();
            if (!monitoringRequested) return;

            EnsureSpotSubscriptions();
            if ((DateTime.UtcNow - lastQuoteUniverseResolveAttemptUtc).TotalSeconds >= QuoteUniverseResolveIntervalSeconds)
                _ = RefreshQuoteUniverseAsync(false);

            if (NeedsAutoRoute() &&
                (DateTime.UtcNow - lastRouteResolveAttemptUtc).TotalSeconds >= RouteResolveIntervalSeconds)
                _ = RefreshAutoRouteAsync(false);

            EnsureSubscription();
            if ((DateTime.UtcNow - lastHeartbeatAttemptUtc).TotalSeconds >= HeartbeatIntervalSeconds)
                _ = SendHeartbeatAsync();
        }

        private void EnsureSubscription()
        {
            var desired = ResolveService();
            var desiredCode = ResolveInstrumentCode();

            if ((IsAutoInstrumentRequested() && string.IsNullOrWhiteSpace(desiredCode)) ||
                (IsAutoServiceRequested() && !autoRouteResolved))
            {
                if (!string.IsNullOrEmpty(activeService))
                    StopSubscription(false, false);
                SetStatusBadge(lblStatus, "시장 정책 확인 중");
                lblResolvedService.Text = "확인 중";
                return;
            }

            if (desired == null)
            {
                if (!string.IsNullOrEmpty(activeService))
                    StopSubscription(false, false);
                activeCode = desiredCode;
                SetStatusBadge(lblStatus, "장외 대기");
                lblResolvedService.Text = "장마감";
                return;
            }

            if (string.Equals(activeService, desired, StringComparison.Ordinal) &&
                string.Equals(activeCode, desiredCode, StringComparison.Ordinal)) return;

            if (!string.IsNullOrEmpty(activeService))
                StopSubscription(false, false);

            Subscribe(desired, desiredCode);
        }

        private void Subscribe(string service, string code)
        {
            try
            {
                activeService = service;
                activeCode = code;
                lblResolvedService.Text = SessionLabelForService(service);
                SetStatusBadge(lblStatus, "구독 요청됨");
                lastTradeError = "";
                tradeFreshTickRequired = true;
                if (!lastTradeCachedObservedUtc.HasValue)
                {
                    lastTradeBusinessTime = "";
                    lastTradePriceText = "";
                    lastTradeRateText = "";
                }
                axTrade.RequestRealData(service, code);
                AppendLog("SUBSCRIBE " + service + " / " + code + " - ReceiveRealData 대기");
            }
            catch (Exception ex)
            {
                activeService = "";
                lastTradeError = ex.GetType().Name + " - " + ex.Message;
                SetStatusBadge(lblStatus, "구독 실패");
                AppendLog("ERROR RequestRealData: " + lastTradeError);
                UpdateMonitorUi();
            }
        }

        private string ResolveService()
        {
            var selected = Convert.ToString(cmbService.SelectedItem) ?? "AUTO";
            if (selected.StartsWith("FC_R", StringComparison.Ordinal)) return "FC_R";
            if (selected.StartsWith("CMEC_R", StringComparison.Ordinal)) return "CMEC_R";
            if (!autoRouteResolved || string.Equals(autoResolvedSession, "closed", StringComparison.Ordinal)) return null;
            return autoResolvedService;
        }

        private bool IsAutoServiceRequested()
        {
            var selected = Convert.ToString(cmbService.SelectedItem) ?? "AUTO";
            return string.Equals(selected, "AUTO", StringComparison.Ordinal);
        }

        private bool NeedsAutoRoute()
        {
            return IsAutoInstrumentRequested() || IsAutoServiceRequested();
        }

        private bool IsAutoInstrumentRequested()
        {
            var requested = (txtCode.Text ?? "AUTO").Trim().ToUpperInvariant();
            return string.Equals(requested, "AUTO", StringComparison.Ordinal);
        }

        private string ResolveInstrumentCode()
        {
            var requested = (txtCode.Text ?? "AUTO").Trim().ToUpperInvariant();
            if (!string.Equals(requested, "AUTO", StringComparison.Ordinal))
                return requested;
            return autoResolvedCode;
        }

        private async Task RefreshAutoRouteAsync(bool force)
        {
            if (!NeedsAutoRoute()) return;
            if (!force && (DateTime.UtcNow - lastRouteResolveAttemptUtc).TotalSeconds < RouteResolveIntervalSeconds) return;
            if (Interlocked.CompareExchange(ref routeResolveInFlight, 1, 0) != 0) return;
            lastRouteResolveAttemptUtc = DateTime.UtcNow;

            try
            {
                using (var response = await httpClient.GetAsync(MarketAiBaseUrl + "/api/bridge/kis-efriend/route-code"))
                {
                    if (!response.IsSuccessStatusCode)
                    {
                        var body = await response.Content.ReadAsStringAsync();
                        InvalidateAutoRoute("route HTTP " + (int)response.StatusCode + " " + body);
                        return;
                    }

                    var raw = (await response.Content.ReadAsStringAsync()).Trim();
                    var parts = raw.Split('|');
                    if (parts.Length != 3)
                    {
                        InvalidateAutoRoute("route invalid payload: " + raw);
                        return;
                    }

                    var code = parts[0].Trim().ToUpperInvariant();
                    var serviceToken = parts[1].Trim().ToUpperInvariant();
                    var session = parts[2].Trim().ToLowerInvariant();
                    var service = serviceToken == "CLOSED" ? "" : serviceToken;
                    var validService =
                        (service == "FC_R" && session == "day") ||
                        (service == "CMEC_R" && session == "night") ||
                        (service.Length == 0 && session == "closed");

                    if (string.IsNullOrWhiteSpace(code) || code.Length > 9 || !validService)
                    {
                        InvalidateAutoRoute("route invalid policy: " + raw);
                        return;
                    }

                    var previousCode = autoResolvedCode;
                    var previousService = autoResolvedService;
                    var previousSession = autoResolvedSession;
                    var changed =
                        !autoRouteResolved ||
                        !string.Equals(previousCode, code, StringComparison.Ordinal) ||
                        !string.Equals(previousService, service, StringComparison.Ordinal) ||
                        !string.Equals(previousSession, session, StringComparison.Ordinal);

                    autoResolvedCode = code;
                    autoResolvedService = service;
                    autoResolvedSession = session;
                    autoRouteResolved = true;
                    lastForwardError = "";

                    SafeUi(() =>
                    {
                        if (changed)
                        {
                            var routeLabel = service.Length == 0 ? "CLOSED" : service;
                            AppendLog("ROUTE " + code + " / " + routeLabel + " / " + session + " (Market AI policy)");
                        }
                        EnsureSubscription();
                        _ = SendHeartbeatAsync();
                    });
                }
            }
            catch (Exception ex)
            {
                InvalidateAutoRoute("route " + ex.GetType().Name + " - " + ex.Message);
            }
            finally
            {
                Interlocked.Exchange(ref routeResolveInFlight, 0);
            }
        }

        private void InvalidateAutoRoute(string message)
        {
            autoRouteResolved = false;
            autoResolvedCode = "";
            autoResolvedService = "";
            autoResolvedSession = "closed";
            MarkForwardError(message);
            SafeUi(() =>
            {
                if (!string.IsNullOrEmpty(activeService))
                    StopSubscription(false, false);
                SetStatusBadge(lblStatus, "시장 정책 확인 중");
                lblResolvedService.Text = "확인 중";
            });
        }

        private void EnsureSpotSubscriptions()
        {
            if (!monitoringRequested) return;
            EnsureSpotSubscription(axKospi, kospiSpot);
            foreach (var registration in quoteStreams.Values)
                EnsureSpotSubscription(registration.Control, registration.State);
            UpdateDomesticStatus();
        }

        private void EnsureSpotSubscription(AxITGExpertCtl control, SpotStreamState state)
        {
            if (state.Subscribed) return;
            try
            {
                control.RequestRealData(state.Service, state.Code);
                state.Subscribed = true;
                state.StreamError = "";
                if (state.UiStatus != null) SetStatusBadge(state.UiStatus, "구독 대기");
                if (state.UiService != null) state.UiService.Text = state.Symbol == "INDEX:KOSPI" ? "정규장" : "-";
                AppendLog("SUBSCRIBE " + state.Name + " " + state.Service + " / " + state.Code + " - ReceiveRealData 대기");
            }
            catch (Exception ex)
            {
                state.StreamError = ex.GetType().Name + " - " + ex.Message;
                if (state.TickCount > 0) state.FreshTickRequired = true;
                if (state.UiStatus != null) SetStatusBadge(state.UiStatus, "구독 오류");
                AppendLog("ERROR " + state.Name + " RequestRealData: " + state.StreamError);
            }
        }

        private void EnsureBaselineQuoteStreams()
        {
            // Only immutable Signal streams are local constants. Dashboard holdings are
            // supplied by Market AI from the actual sibling portfolio/runtime state.
            var desired = new HashSet<string>(SignalBaselineQuoteTickers, StringComparer.Ordinal);
            ApplyQuoteUniverse(
                desired,
                Array.Empty<string>(),
                Array.Empty<string>(),
                new Dictionary<string, string>(StringComparer.Ordinal),
                new Dictionary<string, string>(StringComparer.Ordinal),
                null,
                "signal baseline");
        }

        private async Task RefreshQuoteUniverseAsync(bool force)
        {
            if (!monitoringRequested) return;
            if (!force && (DateTime.UtcNow - lastQuoteUniverseResolveAttemptUtc).TotalSeconds < QuoteUniverseResolveIntervalSeconds) return;
            if (Interlocked.CompareExchange(ref quoteUniverseResolveInFlight, 1, 0) != 0) return;
            lastQuoteUniverseResolveAttemptUtc = DateTime.UtcNow;

            try
            {
                using (var response = await httpClient.GetAsync(MarketAiBaseUrl + QuoteUniversePath))
                {
                    if (!response.IsSuccessStatusCode)
                    {
                        var body = await response.Content.ReadAsStringAsync();
                        MarkQuoteUniverseError("HTTP " + (int)response.StatusCode + " " + body);
                        return;
                    }

                    var raw = await response.Content.ReadAsStringAsync();
                    QuoteUniversePayload payload;
                    try
                    {
                        payload = new JavaScriptSerializer().Deserialize<QuoteUniversePayload>(raw);
                    }
                    catch (Exception ex)
                    {
                        MarkQuoteUniverseError("invalid JSON " + ex.GetType().Name);
                        return;
                    }

                    if (payload == null || payload.tickers == null)
                    {
                        MarkQuoteUniverseError("invalid payload");
                        return;
                    }

                    var desired = new HashSet<string>(SignalBaselineQuoteTickers, StringComparer.Ordinal);
                    foreach (var rawTicker in payload.tickers)
                    {
                        var ticker = NormalizeQuoteTicker(rawTicker);
                        if (ticker == null)
                        {
                            MarkQuoteUniverseError("invalid ticker " + (rawTicker ?? "<null>"));
                            return;
                        }
                        desired.Add(ticker);
                    }

                    var dashboardTickers = new List<string>();
                    var rawDashboardTickers = payload.dashboard_tickers ?? payload.tickers;
                    foreach (var rawTicker in rawDashboardTickers)
                    {
                        var ticker = NormalizeQuoteTicker(rawTicker);
                        if (ticker == null)
                        {
                            MarkQuoteUniverseError("invalid dashboard ticker " + (rawTicker ?? "<null>"));
                            return;
                        }
                        if (!dashboardTickers.Contains(ticker))
                            dashboardTickers.Add(ticker);
                    }

                    var dashboardDisplayTickers = new List<string>();
                    var rawDashboardDisplayTickers = payload.dashboard_display_tickers ?? rawDashboardTickers;
                    foreach (var rawTicker in rawDashboardDisplayTickers)
                    {
                        var ticker = NormalizeQuoteTicker(rawTicker);
                        if (ticker == null)
                        {
                            MarkQuoteUniverseError("invalid dashboard display ticker " + (rawTicker ?? "<null>"));
                            return;
                        }
                        if (!dashboardDisplayTickers.Contains(ticker))
                            dashboardDisplayTickers.Add(ticker);
                    }

                    if (!string.IsNullOrWhiteSpace(payload.market_state))
                        cashMarketState = payload.market_state.Trim().ToLowerInvariant();

                    if (!monitoringRequested) return;
                    quoteUniverseResolved = true;
                    lastQuoteUniverseError = "";
                    SafeUi(() =>
                    {
                        ApplyQuoteUniverse(desired, dashboardTickers, dashboardDisplayTickers, payload.dashboard_names, payload.dashboard_market_states, payload.version, "Market AI");
                        ApplyMonitorSnapshots(payload.monitor_snapshots);
                    });
                }
            }
            catch (Exception ex)
            {
                MarkQuoteUniverseError(ex.GetType().Name + " - " + ex.Message);
            }
            finally
            {
                Interlocked.Exchange(ref quoteUniverseResolveInFlight, 0);
            }
        }

        private void ApplyQuoteUniverse(
            HashSet<string> desiredTickers,
            IEnumerable<string> dashboardTickers,
            IEnumerable<string> dashboardDisplayTickers,
            IDictionary<string, string> dashboardNames,
            IDictionary<string, string> dashboardStates,
            int? version,
            string source)
        {
            if (desiredTickers == null) return;

            dashboardQuoteNames.Clear();
            if (dashboardNames != null)
            {
                foreach (var pair in dashboardNames)
                {
                    var ticker = NormalizeQuoteTicker(pair.Key);
                    var name = (pair.Value ?? "").Trim();
                    if (ticker != null && name.Length > 0)
                        dashboardQuoteNames[ticker] = name;
                }
            }

            dashboardMarketStates.Clear();
            if (dashboardStates != null)
            {
                foreach (var pair in dashboardStates)
                {
                    var ticker = NormalizeQuoteTicker(pair.Key);
                    var marketState = (pair.Value ?? "").Trim().ToLowerInvariant();
                    if (ticker != null && marketState.Length > 0)
                        dashboardMarketStates[ticker] = marketState;
                }
            }

            foreach (var baseline in SignalBaselineQuoteTickers)
                desiredTickers.Add(baseline);

            var remove = new List<string>();
            foreach (var ticker in quoteStreams.Keys)
            {
                if (!desiredTickers.Contains(ticker))
                    remove.Add(ticker);
            }

            foreach (var ticker in remove)
                RemoveQuoteStream(ticker, true);

            foreach (var ticker in desiredTickers)
            {
                if (!quoteStreams.ContainsKey(ticker))
                    AddQuoteStream(ticker);
                QuoteStreamRegistration registration;
                if (quoteStreams.TryGetValue(ticker, out registration))
                {
                    string marketState;
                    registration.State.MarketState = dashboardMarketStates.TryGetValue(ticker, out marketState)
                        ? marketState
                        : "unknown";
                }
            }

            if (monitoringRequested)
            {
                foreach (var registration in quoteStreams.Values)
                    EnsureSpotSubscription(registration.Control, registration.State);
            }

            dashboardQuoteTickers.Clear();
            foreach (var rawTicker in dashboardTickers ?? Array.Empty<string>())
            {
                var ticker = NormalizeQuoteTicker(rawTicker);
                if (ticker != null)
                    dashboardQuoteTickers.Add(ticker);
            }

            dashboardQuoteDisplayOrder.Clear();
            foreach (var rawTicker in dashboardDisplayTickers ?? Array.Empty<string>())
            {
                var ticker = NormalizeQuoteTicker(rawTicker);
                if (ticker != null && dashboardQuoteTickers.Contains(ticker) && !dashboardQuoteDisplayOrder.Contains(ticker))
                    dashboardQuoteDisplayOrder.Add(ticker);
            }

            RebuildHoldingCards();
            UpdateMonitorUi();
        }

        private void ApplyMonitorSnapshots(IEnumerable<MonitorSnapshotPayload> snapshots)
        {
            if (snapshots == null) return;

            foreach (var snapshot in snapshots)
            {
                if (snapshot == null || !snapshot.price.HasValue || snapshot.price.Value <= 0) continue;

                DateTime observedUtc;
                if (!TryParseUtc(snapshot.observed_at, out observedUtc)) continue;

                var businessTime = NormalizeBusinessTime(snapshot.business_time) ??
                    ToKst(observedUtc).ToString("HHmmss", CultureInfo.InvariantCulture);
                var priceText = snapshot.price.Value.ToString("R", CultureInfo.InvariantCulture);
                var rateText = snapshot.change_pct.HasValue
                    ? snapshot.change_pct.Value.ToString("R", CultureInfo.InvariantCulture)
                    : "";
                var changeAmountText = snapshot.change_amount.HasValue
                    ? snapshot.change_amount.Value.ToString("R", CultureInfo.InvariantCulture)
                    : "";
                var symbol = (snapshot.symbol ?? "").Trim().ToUpperInvariant();

                if (string.Equals(symbol, "FUTURES:KOSPI200", StringComparison.Ordinal))
                {
                    if (!lastTickUtc.HasValue || tickCount <= 0)
                    {
                        lastTradeCachedObservedUtc = observedUtc;
                        lastTradeBusinessTime = businessTime;
                        lastTradePriceText = priceText;
                        lastTradeRateText = rateText;
                    }
                    continue;
                }

                if (string.Equals(symbol, "INDEX:KOSPI", StringComparison.Ordinal))
                {
                    SeedCachedSpot(kospiSpot, observedUtc, businessTime, priceText, rateText, changeAmountText);
                    continue;
                }

                if (!symbol.StartsWith("KRX:", StringComparison.Ordinal)) continue;
                var ticker = NormalizeQuoteTicker(symbol.Substring(4));
                if (ticker == null || !dashboardQuoteTickers.Contains(ticker)) continue;

                QuoteStreamRegistration registration;
                if (!quoteStreams.TryGetValue(ticker, out registration)) continue;
                SeedCachedSpot(registration.State, observedUtc, businessTime, priceText, rateText, changeAmountText);
            }

            UpdateMonitorUi();
        }

        private static void SeedCachedSpot(
            SpotStreamState state,
            DateTime observedUtc,
            string businessTime,
            string priceText,
            string rateText,
            string changeAmountText)
        {
            if (state == null || state.LastTickUtc.HasValue || state.TickCount > 0) return;
            state.CachedObservedUtc = observedUtc;
            state.LastBusinessTime = businessTime ?? "";
            state.LastPriceText = priceText ?? "";
            state.LastRateText = rateText ?? "";
            state.LastChangeAmountText = changeAmountText ?? "";
            UpdateSpotUi(state, state.LastBusinessTime, state.LastPriceText, state.LastRateText, null, null, null);
        }

        private static string NormalizeBusinessTime(string value)
        {
            var candidate = (value ?? "").Trim();
            if (candidate.Length != 6) return null;
            for (var i = 0; i < candidate.Length; i++)
            {
                if (candidate[i] < '0' || candidate[i] > '9') return null;
            }
            int hour;
            int minute;
            int second;
            if (!int.TryParse(candidate.Substring(0, 2), out hour) ||
                !int.TryParse(candidate.Substring(2, 2), out minute) ||
                !int.TryParse(candidate.Substring(4, 2), out second) ||
                hour > 23 || minute > 59 || second > 59)
                return null;
            return candidate;
        }

        private static bool TryParseUtc(string value, out DateTime utc)
        {
            DateTime parsed;
            if (!DateTime.TryParse(
                value,
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
                out parsed))
            {
                utc = DateTime.MinValue;
                return false;
            }
            utc = parsed.Kind == DateTimeKind.Utc ? parsed : parsed.ToUniversalTime();
            return true;
        }

        private void AddQuoteStream(string ticker)
        {
            var normalized = NormalizeQuoteTicker(ticker);
            if (normalized == null || quoteStreams.ContainsKey(normalized)) return;

            var control = new AxITGExpertCtl();
            var initializationStarted = false;
            var initializationEnded = false;
            try
            {
                ((ISupportInitialize)control).BeginInit();
                initializationStarted = true;
                var resources = new ComponentResourceManager(typeof(MainForm));
                ConfigureSpotControl(control, resources, "axQuote" + normalized, new Point(832, 730));
                var state = new SpotStreamState(
                    QuoteDisplayName(normalized),
                    "KRX:" + normalized,
                    "SC_R",
                    normalized,
                    1, 2, 4, 5, 13, 10, 11);
                control.ReceiveRealData += (sender, args) => ReceiveSpot(control, state);
                Controls.Add(control);
                control.SendToBack();
                ((ISupportInitialize)control).EndInit();
                initializationEnded = true;
                if (IsHandleCreated && !control.IsHandleCreated)
                    control.CreateControl();
                quoteStreams.Add(normalized, new QuoteStreamRegistration(control, state));
                AppendLog("QUOTE STREAM ADD " + normalized + " / SC_R");
            }
            catch (Exception ex)
            {
                if (initializationStarted && !initializationEnded)
                {
                    try { ((ISupportInitialize)control).EndInit(); } catch { }
                }
                try { Controls.Remove(control); } catch { }
                try { control.Dispose(); } catch { }
                AppendLog("ERROR quote stream " + normalized + " create: " + ex.GetType().Name + " - " + ex.Message);
            }
        }

        private void RemoveQuoteStream(string ticker, bool writeLog)
        {
            QuoteStreamRegistration registration;
            if (!quoteStreams.TryGetValue(ticker, out registration)) return;

            StopSpotSubscription(registration.Control, registration.State, writeLog);
            try { registration.Control.UnRequestAllRealData(); } catch { }
            quoteStreams.Remove(ticker);
            try { Controls.Remove(registration.Control); } catch { }
            try { registration.Control.Dispose(); } catch { }
        }

        private static string NormalizeQuoteTicker(string value)
        {
            var ticker = (value ?? "").Trim().ToUpperInvariant();
            if (ticker.Length != 6) return null;
            for (var i = 0; i < ticker.Length; i++)
            {
                var ch = ticker[i];
                if (!((ch >= '0' && ch <= '9') || (ch >= 'A' && ch <= 'Z')))
                    return null;
            }
            return ticker;
        }

        private string QuoteDisplayName(string ticker)
        {
            string name;
            if (dashboardQuoteNames.TryGetValue(ticker, out name) && !string.IsNullOrWhiteSpace(name))
                return name.Trim();
            return ticker;
        }

        private void MarkQuoteUniverseError(string message)
        {
            var now = DateTime.UtcNow;
            if (!string.Equals(lastQuoteUniverseError, message, StringComparison.Ordinal) ||
                (now - lastQuoteUniverseErrorLogUtc).TotalSeconds >= 60)
            {
                lastQuoteUniverseError = message;
                lastQuoteUniverseErrorLogUtc = now;
                SafeUi(() => AppendLog("QUOTE UNIVERSE WARN " + message + " - existing subscriptions retained"));
            }
            SafeUi(UpdateMonitorUi);
        }

        private void StopSpotSubscriptions(bool writeLog)
        {
            StopSpotSubscription(axKospi, kospiSpot, writeLog);
            foreach (var registration in quoteStreams.Values)
                StopSpotSubscription(registration.Control, registration.State, writeLog);
        }

        private void StopSpotSubscription(AxITGExpertCtl control, SpotStreamState state, bool writeLog)
        {
            if (!state.Subscribed) return;
            try
            {
                control.UnRequestRealData(state.Service, state.Code);
                if (writeLog) AppendLog("UNSUBSCRIBE " + state.Name + " " + state.Service + " / " + state.Code);
            }
            catch (Exception ex)
            {
                if (writeLog) AppendLog("WARN " + state.Name + " UnRequestRealData: " + ex.Message);
            }
            finally
            {
                state.Subscribed = false;
                if (state.TickCount > 0) state.FreshTickRequired = true;
                if (state.UiStatus != null) SetStatusBadge(state.UiStatus, monitoringRequested ? "미구독" : "중지");
            }
        }

        private void AxKospi_ReceiveRealData(object sender, EventArgs e)
        {
            ReceiveSpot(axKospi, kospiSpot);
        }

        private void ReceiveSpot(AxITGExpertCtl control, SpotStreamState state)
        {
            try
            {
                var code = Read(control, 0);
                var time = Read(control, state.TimeIndex);
                var price = Read(control, state.PriceIndex);
                var changeAmount = state.ChangeAmountIndex < 0 ? null : Read(control, state.ChangeAmountIndex);
                var rate = Read(control, state.RateIndex);
                var volume = Read(control, state.VolumeIndex);
                var ask = state.AskIndex < 0 ? null : Read(control, state.AskIndex);
                var bid = state.BidIndex < 0 ? null : Read(control, state.BidIndex);

                state.TickCount++;
                state.LastTickUtc = DateTime.UtcNow;
                state.CachedObservedUtc = null;
                state.StreamError = "";
                state.FreshTickRequired = false;
                state.LastBusinessTime = NormalizeBusinessTime(time) ?? "";
                state.LastPriceText = price ?? "";
                state.LastRateText = rate ?? "";
                state.LastChangeAmountText = changeAmount ?? "";
                UpdateSpotUi(state, state.LastBusinessTime, price, rate, volume, ask, bid);
                if (state.TickCount <= 5 || state.TickCount % 100 == 0)
                {
                    var orderBook = state.AskIndex < 0 ? "" : " ask=" + ask + " bid=" + bid;
                    AppendLog("TICK " + state.Name + " #" + state.TickCount + " " + code + " " + time + " price=" + price + " rate=" + rate + "% vol=" + volume + orderBook);
                }

                UpdateDomesticStatus();
                if ((DateTime.UtcNow - state.LastForwardAttemptUtc).TotalSeconds >= ForwardIntervalSeconds)
                {
                    var snapshot = BuildSpotTickSnapshot(state, code, time, price, changeAmount, rate, volume, ask, bid);
                    if (snapshot != null)
                        _ = SendSpotTickAsync(state, snapshot);
                }
            }
            catch (Exception ex)
            {
                state.StreamError = ex.GetType().Name + " - " + ex.Message;
                if (state.UiStatus != null) SetStatusBadge(state.UiStatus, "수신 오류");
                AppendLog("ERROR " + state.Name + " ReceiveRealData: " + state.StreamError);
                UpdateDomesticStatus();
            }
        }

        private SpotTickSnapshot BuildSpotTickSnapshot(
            SpotStreamState state,
            string code,
            string businessTime,
            string price,
            string changeAmount,
            string rate,
            string volume,
            string ask,
            string bid)
        {
            double numericPrice;
            if (!double.TryParse(price, NumberStyles.Any, CultureInfo.InvariantCulture, out numericPrice) || numericPrice <= 0)
                return null;

            return new SpotTickSnapshot
            {
                Symbol = state.Symbol,
                Code = string.IsNullOrWhiteSpace(code) ? state.Code : code.Trim().ToUpperInvariant(),
                Service = state.Service,
                BusinessTime = NormalizeBusinessTime(businessTime),
                Price = numericPrice,
                ChangeAmount = ParseNullableDouble(changeAmount),
                ChangePct = ParseNullableDouble(rate),
                Volume = ParseNullableLong(volume),
                Ask1 = ParseNullableDouble(ask),
                Bid1 = ParseNullableDouble(bid),
                TickCount = state.TickCount,
                SentAtUtc = DateTime.UtcNow
            };
        }

        private async Task SendSpotTickAsync(SpotStreamState state, SpotTickSnapshot tick)
        {
            if (Interlocked.CompareExchange(ref state.ForwardInFlight, 1, 0) != 0) return;
            state.LastForwardAttemptUtc = DateTime.UtcNow;

            try
            {
                var json = BuildSpotTickJson(tick);
                using (var content = new StringContent(json, Encoding.UTF8, "application/json"))
                using (var response = await httpClient.PostAsync(MarketAiBaseUrl + "/api/bridge/kis-efriend/market-tick", content))
                {
                    if (!response.IsSuccessStatusCode)
                    {
                        var body = await response.Content.ReadAsStringAsync();
                        var forwardError = "HTTP " + (int)response.StatusCode + " " + body;
                        state.ForwardError = forwardError;
                        SafeUi(() =>
                        {
                            if (state.UiStatus != null && string.IsNullOrEmpty(state.StreamError))
                                SetStatusBadge(state.UiStatus, "AI 전송 오류");
                            AppendLog("MARKET AI ERROR " + state.Name + " " + forwardError);
                        });
                        return;
                    }
                }

                state.ForwardSuccessCount++;
                state.LastForwardedTickCount = tick.TickCount;
                state.ForwardError = "";
                SafeUi(() =>
                {
                    if (state.UiStatus != null &&
                        string.IsNullOrEmpty(state.StreamError) &&
                        string.IsNullOrEmpty(state.ForwardError))
                        SetStatusBadge(state.UiStatus, "수신 중");
                });
                if (state.ForwardSuccessCount <= 3 || state.ForwardSuccessCount % 50 == 0)
                    SafeUi(() => AppendLog("MARKET AI OK " + state.Name + " tick=" + tick.TickCount + " price=" + tick.Price.ToString("0.00", CultureInfo.InvariantCulture)));
            }
            catch (Exception ex)
            {
                var forwardError = ex.GetType().Name + " - " + ex.Message;
                state.ForwardError = forwardError;
                SafeUi(() =>
                {
                    if (state.UiStatus != null && string.IsNullOrEmpty(state.StreamError))
                        SetStatusBadge(state.UiStatus, "AI 전송 오류");
                    AppendLog("MARKET AI ERROR " + state.Name + " " + forwardError);
                });
            }
            finally
            {
                Interlocked.Exchange(ref state.ForwardInFlight, 0);
                SafeUi(UpdateDomesticStatus);
            }
        }

        private void UpdateDomesticStatus()
        {
            UpdateMonitorUi();
        }

        private void AxTrade_ReceiveRealData(object sender, EventArgs e)
        {
            try
            {
                // FC_R / CMEC_R are intentionally parsed with the same indexes confirmed in Expert Viewer.
                var code = Read(0);
                var time = Read(1);
                var rate = Read(4);
                var price = Read(5);
                var volume = Read(10);
                var ask = Read(34);
                var bid = Read(35);

                tickCount++;
                lastTickUtc = DateTime.UtcNow;
                lastTradeCachedObservedUtc = null;
                lastTradeError = "";
                tradeFreshTickRequired = false;
                lastTradeBusinessTime = NormalizeBusinessTime(time) ?? "";
                lastTradePriceText = price ?? "";
                lastTradeRateText = rate ?? "";
                SetStatusBadge(lblStatus, "수신 중");
                lblTime.Text = FormatBusinessTime(time);
                lblPrice.Text = FormatNumber(price, 2);
                lblChangeRate.Text = FormatSignedPercent(rate);
                ApplyRateColor(lblChangeRate, rate);

                if (tickCount <= 5 || tickCount % 100 == 0)
                    AppendLog("TICK " + tickCount + " " + code + " " + time + " price=" + price + " rate=" + rate + "% vol=" + volume + " ask=" + ask + " bid=" + bid);

                if ((DateTime.UtcNow - lastForwardAttemptUtc).TotalSeconds >= ForwardIntervalSeconds)
                {
                    var snapshot = BuildTickSnapshot(code, time, price, rate, volume, ask, bid);
                    if (snapshot != null)
                        _ = SendTickAsync(snapshot);
                }
                UpdateMonitorUi();
            }
            catch (Exception ex)
            {
                lastTradeError = ex.GetType().Name + " - " + ex.Message;
                SetStatusBadge(lblStatus, "수신 파싱 오류");
                AppendLog("ERROR ReceiveRealData: " + lastTradeError);
                UpdateMonitorUi();
            }
        }

        private TickSnapshot BuildTickSnapshot(string code, string businessTime, string price, string rate, string volume, string ask, string bid)
        {
            double numericPrice;
            if (!double.TryParse(price, NumberStyles.Any, CultureInfo.InvariantCulture, out numericPrice) || numericPrice <= 0)
                return null;

            return new TickSnapshot
            {
                Code = string.IsNullOrWhiteSpace(code) ? activeCode : code.Trim().ToUpperInvariant(),
                Service = activeService,
                Session = string.Equals(activeService, "FC_R", StringComparison.Ordinal) ? "day" : "night",
                BusinessTime = NormalizeBusinessTime(businessTime),
                Price = numericPrice,
                ChangePct = ParseNullableDouble(rate),
                Volume = ParseNullableLong(volume),
                Ask1 = ParseNullableDouble(ask),
                Bid1 = ParseNullableDouble(bid),
                TickCount = tickCount,
                SentAtUtc = DateTime.UtcNow
            };
        }

        private async Task SendTickAsync(TickSnapshot tick)
        {
            if (Interlocked.CompareExchange(ref forwardInFlight, 1, 0) != 0) return;
            lastForwardAttemptUtc = DateTime.UtcNow;

            try
            {
                var json = BuildTickJson(tick);
                using (var content = new StringContent(json, Encoding.UTF8, "application/json"))
                using (var response = await httpClient.PostAsync(MarketAiBaseUrl + "/api/bridge/kis-efriend/tick", content))
                {
                    if (!response.IsSuccessStatusCode)
                    {
                        var body = await response.Content.ReadAsStringAsync();
                        MarkForwardError("HTTP " + (int)response.StatusCode + " " + body);
                        return;
                    }
                }

                forwardSuccessCount++;
                lastForwardError = "";
                SafeUi(UpdateMonitorUi);
                if (forwardSuccessCount <= 3 || forwardSuccessCount % 50 == 0)
                    SafeUi(() => AppendLog("MARKET AI OK tick=" + tick.TickCount + " price=" + tick.Price.ToString("0.00", CultureInfo.InvariantCulture)));
            }
            catch (Exception ex)
            {
                MarkForwardError(ex.GetType().Name + " - " + ex.Message);
            }
            finally
            {
                Interlocked.Exchange(ref forwardInFlight, 0);
            }
        }

        private async Task SendHeartbeatAsync()
        {
            // Heartbeats are full SC_R subscription-health snapshots. Never allow
            // concurrent HTTP posts from this Bridge process because an older
            // unhealthy snapshot could otherwise arrive after a newer recovery.
            if (Interlocked.CompareExchange(ref heartbeatInFlight, 1, 0) != 0) return;

            try
            {
                lastHeartbeatAttemptUtc = DateTime.UtcNow;
                var desired = ResolveService();
                var service = string.IsNullOrEmpty(activeService) ? desired : activeService;
                var session = service == "FC_R" ? "day" : service == "CMEC_R" ? "night" : "closed";
                if (IsAutoServiceRequested() && autoRouteResolved && string.IsNullOrEmpty(activeService))
                    session = autoResolvedSession;

                var heartbeatCode = string.IsNullOrWhiteSpace(activeCode) ? ResolveInstrumentCode() : activeCode;
                if (string.IsNullOrWhiteSpace(heartbeatCode)) return;
                var json = BuildHeartbeatJson(
                    heartbeatCode,
                    service,
                    session,
                    tickCount,
                    lastTickUtc
                );

                using (var content = new StringContent(json, Encoding.UTF8, "application/json"))
                using (var response = await httpClient.PostAsync(MarketAiBaseUrl + "/api/bridge/kis-efriend/heartbeat", content))
                {
                    if (!response.IsSuccessStatusCode)
                    {
                        var body = await response.Content.ReadAsStringAsync();
                        MarkForwardError("heartbeat HTTP " + (int)response.StatusCode + " " + body);
                        return;
                    }
                }
                lastForwardError = "";
                SafeUi(UpdateMonitorUi);
            }
            catch (Exception ex)
            {
                MarkForwardError("heartbeat " + ex.GetType().Name + " - " + ex.Message);
            }
            finally
            {
                Interlocked.Exchange(ref heartbeatInFlight, 0);
            }
        }

        private void MarkForwardError(string message)
        {
            var now = DateTime.UtcNow;
            if (!string.Equals(lastForwardError, message, StringComparison.Ordinal) || (now - lastForwardErrorLogUtc).TotalSeconds >= 30)
            {
                lastForwardError = message;
                lastForwardErrorLogUtc = now;
                SafeUi(() => AppendLog("MARKET AI ERROR " + message));
            }
            SafeUi(UpdateMonitorUi);
        }

        private static string BuildTickJson(TickSnapshot tick)
        {
            return "{" +
                   "\"instrument_code\":\"" + JsonEscape(tick.Code) + "\"," +
                   "\"service\":\"" + JsonEscape(tick.Service) + "\"," +
                   "\"session\":\"" + JsonEscape(tick.Session) + "\"," +
                   "\"business_time\":" + JsonNullableString(tick.BusinessTime) + "," +
                   "\"price\":" + tick.Price.ToString("R", CultureInfo.InvariantCulture) + "," +
                   "\"change_pct\":" + JsonNullable(tick.ChangePct) + "," +
                   "\"cumulative_volume\":" + JsonNullable(tick.Volume) + "," +
                   "\"ask1\":" + JsonNullable(tick.Ask1) + "," +
                   "\"bid1\":" + JsonNullable(tick.Bid1) + "," +
                   "\"sent_at\":\"" + tick.SentAtUtc.ToString("o", CultureInfo.InvariantCulture) + "\"," +
                   "\"tick_count\":" + tick.TickCount.ToString(CultureInfo.InvariantCulture) +
                   "}";
        }

        private static string BuildSpotTickJson(SpotTickSnapshot tick)
        {
            return "{" +
                   "\"symbol\":\"" + JsonEscape(tick.Symbol) + "\"," +
                   "\"instrument_code\":\"" + JsonEscape(tick.Code) + "\"," +
                   "\"service\":\"" + JsonEscape(tick.Service) + "\"," +
                   "\"business_time\":" + JsonNullableString(tick.BusinessTime) + "," +
                   "\"price\":" + tick.Price.ToString("R", CultureInfo.InvariantCulture) + "," +
                   "\"change_amount\":" + JsonNullable(tick.ChangeAmount) + "," +
                   "\"change_pct\":" + JsonNullable(tick.ChangePct) + "," +
                   "\"cumulative_volume\":" + JsonNullable(tick.Volume) + "," +
                   "\"ask1\":" + JsonNullable(tick.Ask1) + "," +
                   "\"bid1\":" + JsonNullable(tick.Bid1) + "," +
                   "\"sent_at\":\"" + tick.SentAtUtc.ToString("o", CultureInfo.InvariantCulture) + "\"," +
                   "\"tick_count\":" + tick.TickCount.ToString(CultureInfo.InvariantCulture) +
                   "}";
        }

        private string BuildHeartbeatJson(string code, string service, string session, long ticks, DateTime? lastTick)
        {
            return "{" +
                   "\"instrument_code\":\"" + JsonEscape(code) + "\"," +
                   "\"service\":" + (service == null ? "null" : "\"" + JsonEscape(service) + "\"") + "," +
                   "\"session\":\"" + JsonEscape(session) + "\"," +
                   "\"bridge_time\":\"" + DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) + "\"," +
                   "\"last_tick_at\":" + (lastTick.HasValue ? "\"" + lastTick.Value.ToString("o", CultureInfo.InvariantCulture) + "\"" : "null") + "," +
                   "\"tick_count\":" + ticks.ToString(CultureInfo.InvariantCulture) + "," +
                   "\"quote_subscriptions\":" + BuildQuoteSubscriptionsJson() +
                   "}";
        }

        private string BuildQuoteSubscriptionsJson()
        {
            var json = new StringBuilder();
            json.Append('[');
            var first = true;
            foreach (var pair in quoteStreams)
            {
                if (!first) json.Append(',');
                first = false;
                var state = pair.Value.State;
                json.Append('{');
                json.Append("\"ticker\":\"").Append(JsonEscape(pair.Key)).Append("\",");
                json.Append("\"subscribed\":").Append(state.Subscribed ? "true" : "false").Append(',');
                json.Append("\"last_tick_at\":");
                if (state.LastTickUtc.HasValue)
                    json.Append('\"').Append(state.LastTickUtc.Value.ToString("o", CultureInfo.InvariantCulture)).Append('\"');
                else
                    json.Append("null");
                json.Append(',');
                json.Append("\"tick_count\":").Append(state.TickCount.ToString(CultureInfo.InvariantCulture)).Append(',');
                json.Append("\"forward_success_count\":").Append(state.ForwardSuccessCount.ToString(CultureInfo.InvariantCulture)).Append(',');
                json.Append("\"last_forwarded_tick_count\":");
                if (state.LastForwardedTickCount.HasValue)
                    json.Append(state.LastForwardedTickCount.Value.ToString(CultureInfo.InvariantCulture));
                else
                    json.Append("null");
                json.Append(',');
                json.Append("\"last_error\":");
                if (string.IsNullOrEmpty(state.LastError))
                    json.Append("null");
                else
                    json.Append('\"').Append(JsonEscape(state.LastError)).Append('\"');
                json.Append('}');
            }
            json.Append(']');
            return json.ToString();
        }

        private static string JsonEscape(string value)
        {
            return (value ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n");
        }

        private static string JsonNullableString(string value)
        {
            return string.IsNullOrWhiteSpace(value) ? "null" : "\"" + JsonEscape(value) + "\"";
        }

        private static string JsonNullable(double? value)
        {
            return value.HasValue ? value.Value.ToString("R", CultureInfo.InvariantCulture) : "null";
        }

        private static string JsonNullable(long? value)
        {
            return value.HasValue ? value.Value.ToString(CultureInfo.InvariantCulture) : "null";
        }

        private static double? ParseNullableDouble(string value)
        {
            double parsed;
            return double.TryParse(value, NumberStyles.Any, CultureInfo.InvariantCulture, out parsed) ? parsed : (double?)null;
        }

        private static long? ParseNullableLong(string value)
        {
            long parsed;
            return long.TryParse(value, NumberStyles.Any, CultureInfo.InvariantCulture, out parsed) ? parsed : (long?)null;
        }

        private void SafeUi(Action action)
        {
            if (IsDisposed || Disposing) return;
            if (InvokeRequired)
            {
                try { BeginInvoke(action); } catch { }
                return;
            }
            action();
        }

        private string Read(short index)
        {
            return Convert.ToString(axTrade.GetSingleData(index, 0), CultureInfo.InvariantCulture)?.Trim() ?? "";
        }

        private static string Read(AxITGExpertCtl control, short index)
        {
            return Convert.ToString(control.GetSingleData(index, 0), CultureInfo.InvariantCulture)?.Trim() ?? "";
        }

        private void StopSubscription(bool writeLog, bool resetControls)
        {
            if (!string.IsNullOrEmpty(activeService) && !string.IsNullOrEmpty(activeCode))
            {
                try
                {
                    axTrade.UnRequestRealData(activeService, activeCode);
                    if (writeLog) AppendLog("UNSUBSCRIBE " + activeService + " / " + activeCode);
                }
                catch (Exception ex)
                {
                    if (writeLog) AppendLog("WARN UnRequestRealData: " + ex.Message);
                }
            }

            activeService = "";
            if (resetControls)
            {
                SetStatusBadge(lblStatus, "중지");
                lblResolvedService.Text = "-";
                btnStart.Enabled = true;
                btnStop.Enabled = false;
                txtCode.Enabled = true;
                cmbService.Enabled = true;
            }
        }

        private void MainForm_FormClosing(object sender, FormClosingEventArgs e)
        {
            // WndProc handles the normal title-bar X / Alt+F4 path before Close()
            // begins. Keep this as a defensive fallback for any user-close path
            // that bypasses WM_SYSCOMMAND.
            if (e.CloseReason == CloseReason.UserClosing && !exitRequested)
            {
                e.Cancel = true;
                HideBridgeWindow();
                return;
            }

            monitoringRequested = false;
            sessionTimer.Stop();
            try { axTrade.UnRequestAllRealData(); } catch { }
            try { axKospi.UnRequestAllRealData(); } catch { }
            foreach (var registration in quoteStreams.Values)
            {
                try { registration.Control.UnRequestAllRealData(); } catch { }
                try { registration.Control.Dispose(); } catch { }
            }
            quoteStreams.Clear();
            httpClient.Dispose();
        }

        private void AppendLog(string text)
        {
            var line = DateTime.Now.ToString("HH:mm:ss.fff", CultureInfo.InvariantCulture) + "  " + text + Environment.NewLine;
            txtLog.AppendText(line);
            txtLog.SelectionStart = txtLog.TextLength;
            txtLog.ScrollToCaret();
        }

        private static string FormatBusinessTime(string value)
        {
            var normalized = NormalizeBusinessTime(value);
            if (normalized == null) return "--:--:--";
            return normalized.Substring(0, 2) + ":" + normalized.Substring(2, 2) + ":" + normalized.Substring(4, 2);
        }

        private static string FormatSignedPercent(string value)
        {
            double number;
            if (double.TryParse(value, NumberStyles.Any, CultureInfo.InvariantCulture, out number))
                return (number > 0 ? "+" : "") + number.ToString("N2", CultureInfo.InvariantCulture) + "%";
            return string.IsNullOrWhiteSpace(value) ? "-" : value;
        }

        private static string FormatHoldingChange(string rateValue, string changeAmountValue)
        {
            var percent = FormatSignedPercent(rateValue);
            if (percent == "-" || string.IsNullOrWhiteSpace(changeAmountValue)) return percent;

            double amount;
            if (!double.TryParse(changeAmountValue, NumberStyles.Any, CultureInfo.InvariantCulture, out amount))
                return percent;

            var sign = amount > 0 ? "+" : "";
            return percent + "  " + sign + amount.ToString("N0", CultureInfo.InvariantCulture);
        }

        private static string FormatNumber(string value, int decimals)
        {
            double number;
            if (double.TryParse(value, NumberStyles.Any, CultureInfo.InvariantCulture, out number))
                return number.ToString("N" + decimals, CultureInfo.InvariantCulture);
            return string.IsNullOrWhiteSpace(value) ? "-" : value;
        }

        private static string FormatInteger(string value)
        {
            long number;
            if (long.TryParse(value, NumberStyles.Any, CultureInfo.InvariantCulture, out number))
                return number.ToString("N0", CultureInfo.InvariantCulture);
            return string.IsNullOrWhiteSpace(value) ? "-" : value;
        }

        private sealed class MarketCardUi
        {
            public MarketCardUi(
                RoundedPanel container,
                Label status,
                Label service,
                Label time,
                Label price,
                Label rate)
            {
                Container = container;
                Status = status;
                Service = service;
                Time = time;
                Price = price;
                Rate = rate;
            }

            public RoundedPanel Container { get; private set; }
            public Label Status { get; private set; }
            public Label Service { get; private set; }
            public Label Time { get; private set; }
            public Label Price { get; private set; }
            public Label Rate { get; private set; }
        }

        private sealed class HoldingQuoteCard
        {
            public HoldingQuoteCard(
                string ticker,
                RoundedPanel container,
                Label nameLabel,
                Label status,
                Label time,
                Label price,
                Label rate)
            {
                Ticker = ticker;
                Container = container;
                NameLabel = nameLabel;
                Status = status;
                Time = time;
                Price = price;
                Rate = rate;
            }

            public string Ticker { get; private set; }
            public RoundedPanel Container { get; private set; }
            public Label NameLabel { get; private set; }
            public Label Status { get; private set; }
            public Label Time { get; private set; }
            public Label Price { get; private set; }
            public Label Rate { get; private set; }
        }

        private sealed class RoundedPanel : Panel
        {
            private int radius = 12;
            private Color borderColor = UiBorder;
            private Color fillColor = UiSurface;

            public RoundedPanel()
            {
                SetStyle(
                    ControlStyles.AllPaintingInWmPaint |
                    ControlStyles.OptimizedDoubleBuffer |
                    ControlStyles.ResizeRedraw |
                    ControlStyles.UserPaint |
                    ControlStyles.SupportsTransparentBackColor,
                    true);
                BackColor = Color.Transparent;
            }

            public int Radius
            {
                get { return radius; }
                set
                {
                    radius = Math.Max(0, value);
                    UpdateRegion();
                    Invalidate();
                }
            }

            public Color BorderColor
            {
                get { return borderColor; }
                set
                {
                    borderColor = value;
                    Invalidate();
                }
            }

            public Color FillColor
            {
                get { return fillColor; }
                set
                {
                    fillColor = value;
                    Invalidate();
                }
            }

            protected override void OnResize(EventArgs eventargs)
            {
                base.OnResize(eventargs);
                UpdateRegion();
            }

            protected override void OnPaintBackground(PaintEventArgs e)
            {
                // Let WinForms composite the transparent corners from the parent first.
                // Without this, the double-buffer surface can remain black outside the
                // rounded fill path and appear as clipped wedges at the top/right edges.
                base.OnPaintBackground(e);

                e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                var rect = ClientRectangle;
                if (rect.Width <= 0 || rect.Height <= 0) return;
                rect.Width -= 1;
                rect.Height -= 1;
                using (var path = CreateRoundedPath(rect, Radius))
                using (var brush = new SolidBrush(FillColor))
                {
                    e.Graphics.FillPath(brush, path);
                }
            }

            protected override void OnPaint(PaintEventArgs e)
            {
                base.OnPaint(e);
                e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                var rect = ClientRectangle;
                if (rect.Width <= 1 || rect.Height <= 1) return;
                rect.Width -= 1;
                rect.Height -= 1;
                using (var path = CreateRoundedPath(rect, Radius))
                using (var pen = new Pen(BorderColor, 1F))
                {
                    e.Graphics.DrawPath(pen, path);
                }
            }

            private void UpdateRegion()
            {
                if (ClientSize.Width <= 0 || ClientSize.Height <= 0) return;
                var rect = ClientRectangle;
                using (var path = CreateRoundedPath(rect, Radius))
                {
                    var previous = Region;
                    Region = new Region(path);
                    if (previous != null) previous.Dispose();
                }
            }
        }

        private sealed class PillLabel : Label
        {
            private int radius = 12;
            private Color fillColor = UiWarmingBg;

            public PillLabel()
            {
                SetStyle(
                    ControlStyles.AllPaintingInWmPaint |
                    ControlStyles.OptimizedDoubleBuffer |
                    ControlStyles.ResizeRedraw |
                    ControlStyles.UserPaint |
                    ControlStyles.SupportsTransparentBackColor,
                    true);
                BackColor = Color.Transparent;
            }

            public int Radius
            {
                get { return radius; }
                set
                {
                    radius = Math.Max(0, value);
                    Invalidate();
                }
            }

            public Color FillColor
            {
                get { return fillColor; }
                set
                {
                    fillColor = value;
                    Invalidate();
                }
            }

            protected override void OnPaintBackground(PaintEventArgs e)
            {
                // Let WinForms composite the transparent corners from the parent first.
                // Without this, the double-buffer surface can remain black outside the
                // rounded fill path and appear as clipped wedges at the top/right edges.
                base.OnPaintBackground(e);

                e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                var rect = ClientRectangle;
                if (rect.Width <= 0 || rect.Height <= 0) return;
                rect.Width -= 1;
                rect.Height -= 1;
                using (var path = CreateRoundedPath(rect, Radius))
                using (var brush = new SolidBrush(FillColor))
                {
                    e.Graphics.FillPath(brush, path);
                }
            }

            protected override void OnPaint(PaintEventArgs e)
            {
                TextRenderer.DrawText(
                    e.Graphics,
                    Text,
                    Font,
                    ClientRectangle,
                    ForeColor,
                    TextFormatFlags.HorizontalCenter |
                    TextFormatFlags.VerticalCenter |
                    TextFormatFlags.EndEllipsis |
                    TextFormatFlags.NoPadding);
            }
        }

        private static GraphicsPath CreateRoundedPath(Rectangle rectangle, int radius)
        {
            var path = new GraphicsPath();
            if (radius <= 0)
            {
                path.AddRectangle(rectangle);
                path.CloseFigure();
                return path;
            }

            var diameter = Math.Min(radius * 2, Math.Min(rectangle.Width, rectangle.Height));
            if (diameter <= 0)
            {
                path.AddRectangle(rectangle);
                path.CloseFigure();
                return path;
            }

            var arc = new Rectangle(rectangle.Location, new Size(diameter, diameter));
            path.AddArc(arc, 180, 90);
            arc.X = rectangle.Right - diameter;
            path.AddArc(arc, 270, 90);
            arc.Y = rectangle.Bottom - diameter;
            path.AddArc(arc, 0, 90);
            arc.X = rectangle.Left;
            path.AddArc(arc, 90, 90);
            path.CloseFigure();
            return path;
        }

        private sealed class QuoteUniversePayload
        {
            public QuoteUniversePayload() { }
            public int version { get; set; }
            public string[] tickers { get; set; }
            public string[] dashboard_tickers { get; set; }
            public string[] dashboard_display_tickers { get; set; }
            public Dictionary<string, string> dashboard_names { get; set; }
            public Dictionary<string, string> dashboard_market_states { get; set; }
            public string[] signal_baseline_tickers { get; set; }
            public string market_state { get; set; }
            public bool bootstrap_active { get; set; }
            public MonitorSnapshotPayload[] monitor_snapshots { get; set; }
        }

        private sealed class MonitorSnapshotPayload
        {
            public MonitorSnapshotPayload() { }
            public string symbol { get; set; }
            public double? price { get; set; }
            public double? change_pct { get; set; }
            public double? change_amount { get; set; }
            public string source { get; set; }
            public string observed_at { get; set; }
            public string business_time { get; set; }
        }

        private sealed class QuoteStreamRegistration
        {
            public QuoteStreamRegistration(AxITGExpertCtl control, SpotStreamState state)
            {
                Control = control;
                State = state;
            }

            public AxITGExpertCtl Control { get; private set; }
            public SpotStreamState State { get; private set; }
        }

        private sealed class SpotStreamState
        {
            public SpotStreamState(
                string name,
                string symbol,
                string service,
                string code,
                short timeIndex,
                short priceIndex,
                short changeAmountIndex,
                short rateIndex,
                short volumeIndex,
                short askIndex,
                short bidIndex)
            {
                Name = name;
                Symbol = symbol;
                Service = service;
                Code = code;
                TimeIndex = timeIndex;
                PriceIndex = priceIndex;
                ChangeAmountIndex = changeAmountIndex;
                RateIndex = rateIndex;
                VolumeIndex = volumeIndex;
                AskIndex = askIndex;
                BidIndex = bidIndex;
            }

            public string Name { get; private set; }
            public string Symbol { get; private set; }
            public string Service { get; private set; }
            public string Code { get; private set; }
            public short TimeIndex { get; private set; }
            public short PriceIndex { get; private set; }
            public short ChangeAmountIndex { get; private set; }
            public short RateIndex { get; private set; }
            public short VolumeIndex { get; private set; }
            public short AskIndex { get; private set; }
            public short BidIndex { get; private set; }
            public bool Subscribed { get; set; }
            public long TickCount { get; set; }
            public long ForwardSuccessCount { get; set; }
            public long? LastForwardedTickCount { get; set; }
            public int ForwardInFlight;
            public DateTime LastForwardAttemptUtc { get; set; } = DateTime.MinValue;
            public DateTime? LastTickUtc { get; set; }
            public DateTime? CachedObservedUtc { get; set; }
            public string StreamError { get; set; } = "";
            public string ForwardError { get; set; } = "";
            public string LastError
            {
                get
                {
                    if (!string.IsNullOrEmpty(StreamError)) return StreamError;
                    return ForwardError ?? "";
                }
            }
            public bool FreshTickRequired { get; set; }
            public string MarketState { get; set; } = "unknown";
            public string LastBusinessTime { get; set; } = "";
            public string LastPriceText { get; set; } = "";
            public string LastRateText { get; set; } = "";
            public string LastChangeAmountText { get; set; } = "";
            public Label UiStatus { get; set; }
            public Label UiService { get; set; }
            public Label UiTime { get; set; }
            public Label UiPrice { get; set; }
            public Label UiRate { get; set; }
        }

        private sealed class SpotTickSnapshot
        {
            public string Symbol { get; set; }
            public string Code { get; set; }
            public string Service { get; set; }
            public string BusinessTime { get; set; }
            public double Price { get; set; }
            public double? ChangeAmount { get; set; }
            public double? ChangePct { get; set; }
            public long? Volume { get; set; }
            public double? Ask1 { get; set; }
            public double? Bid1 { get; set; }
            public long TickCount { get; set; }
            public DateTime SentAtUtc { get; set; }
        }

        private sealed class TickSnapshot
        {
            public string Code { get; set; }
            public string Service { get; set; }
            public string Session { get; set; }
            public string BusinessTime { get; set; }
            public double Price { get; set; }
            public double? ChangePct { get; set; }
            public long? Volume { get; set; }
            public double? Ask1 { get; set; }
            public double? Bid1 { get; set; }
            public long TickCount { get; set; }
            public DateTime SentAtUtc { get; set; }
        }
    }
}
