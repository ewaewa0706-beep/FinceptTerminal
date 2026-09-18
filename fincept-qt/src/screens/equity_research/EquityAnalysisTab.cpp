// src/screens/equity_research/EquityAnalysisTab.cpp
//
// The "Analysis" sub-tab interprets StockInfo into decisions rather than
// listing it (Overview already lists). Layout: a full-width analyst
// price-target gauge (hero) above a 3×2 grid of color-coded verdict cards
// (Valuation, Financial Health, Cash Flow, Profitability, Growth, Risk).
//
// Every built-in rating is computed purely from StockInfo. Korean listings also
// expose an explicit user-triggered deep-research panel at the bottom; it makes
// no call until the user presses the button.
// The heuristics are absolute screening signals (not sector-adjusted), so
// rationales stay factual. i18n follows the existing pattern: static labels
// register an English source key in i18n_labels_; dynamic verdict/hero text
// is rebuilt via tr() each time on_info_loaded() runs, and retranslateUi()
// replays it on a language change.
#include "screens/equity_research/EquityAnalysisTab.h"

#include "python/PythonRunner.h"
#include "services/equity/PersonalKrLlmConfig.h"
#include "services/equity/EquityResearchService.h"
#include "ui/theme/Theme.h"

#include <QDate>
#include <QDateEdit>
#include <QDateTime>
#include <QComboBox>
#include <QDoubleSpinBox>
#include <QEvent>
#include <QFontMetrics>
#include <QFrame>
#include <QGridLayout>
#include <QHeaderView>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMessageBox>
#include <QPainter>
#include <QPlainTextEdit>
#include <QPointer>
#include <QPolygonF>
#include <QPushButton>
#include <QScrollArea>
#include <QSpinBox>
#include <QTableWidget>
#include <QUuid>
#include <QVBoxLayout>

#include <algorithm>

namespace fincept::screens {

// ── AnalysisPriceTargetGauge ───────────────────────────────────────────────────
// Custom-painted low—mean—high track with a clamped "now" marker. No Q_OBJECT
// (no signals/slots) so it needs no moc and is safe in the unity build. Numeric
// labels are drawn directly; the big price / upside / recommendation text lives
// in sibling QLabels owned by the tab.
class AnalysisPriceTargetGauge : public QWidget {
  public:
    explicit AnalysisPriceTargetGauge(QWidget* parent = nullptr) : QWidget(parent) {
        setMinimumHeight(88);
        setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    }

    void set_data(double low, double mean, double high, double price, const QString& cur) {
        low_ = low;
        mean_ = mean;
        high_ = high;
        price_ = price;
        cur_ = cur;
        has_data_ = (low > 0.0 || mean > 0.0 || high > 0.0);
        update();
    }

    void clear_data() {
        has_data_ = false;
        update();
    }

  protected:
    void paintEvent(QPaintEvent*) override {
        if (!has_data_)
            return;

        QPainter p(this);
        p.setRenderHint(QPainter::Antialiasing, true);

        const double w = width();
        const double margin = 12.0;
        double x_left = margin;
        double x_right = w - margin;
        if (x_right <= x_left + 1.0) {
            x_left = 2.0;
            x_right = w - 2.0;
        }
        const double y_track = std::round(height() * 0.52);
        const double track_h = 6.0;

        auto x_for = [&](double v) -> double {
            if (high_ <= low_)
                return (x_left + x_right) / 2.0;
            double t = (v - low_) / (high_ - low_);
            t = std::clamp(t, 0.0, 1.0);
            return x_left + (t * (x_right - x_left));
        };

        // Base track.
        QRectF track(x_left, y_track - (track_h / 2.0), x_right - x_left, track_h);
        p.setPen(Qt::NoPen);
        p.setBrush(QColor(ui::colors::BORDER_MED()));
        p.drawRoundedRect(track, 3, 3);

        // Filled portion from low → now (visualizes where price sits in range).
        if (price_ > 0.0) {
            double xp = x_for(price_);
            QRectF fill(x_left, y_track - (track_h / 2.0), xp - x_left, track_h);
            p.setBrush(QColor(ui::colors::CYAN()));
            p.drawRoundedRect(fill, 3, 3);
        }

        QFont small = font();
        small.setPointSizeF(std::max(10.0, small.pointSizeF() + 1.0));
        p.setFont(small);
        const QFontMetrics fm(small);

        auto money = [&](double v) -> QString {
            const int dec = v >= 1000.0 ? 0 : 2;
            return cur_ + QString::number(v, 'f', dec);
        };

        // Mean tick + label (above the track).
        if (mean_ > 0.0) {
            double xm = x_for(mean_);
            p.setPen(QPen(QColor(ui::colors::AMBER()), 1.5));
            p.drawLine(QPointF(xm, y_track - 9), QPointF(xm, y_track + 9));
            const QString t = QStringLiteral("mean ") + money(mean_);
            double tx = std::clamp(xm - (fm.horizontalAdvance(t) / 2.0), x_left, x_right - fm.horizontalAdvance(t));
            p.setPen(QColor(ui::colors::AMBER()));
            p.drawText(QPointF(tx, y_track - 12), t);
        }

        // Low / high labels (below the track ends).
        p.setPen(QColor(ui::colors::TEXT_TERTIARY()));
        if (low_ > 0.0)
            p.drawText(QPointF(x_left, y_track + 20), QStringLiteral("low ") + money(low_));
        if (high_ > 0.0) {
            const QString t = QStringLiteral("high ") + money(high_);
            p.drawText(QPointF(x_right - fm.horizontalAdvance(t), y_track + 20), t);
        }

        // "Now" marker: downward triangle sitting on the track.
        if (price_ > 0.0) {
            double xp = x_for(price_);
            QPolygonF tri;
            tri << QPointF(xp - 5, y_track - 10) << QPointF(xp + 5, y_track - 10) << QPointF(xp, y_track - 2);
            p.setPen(Qt::NoPen);
            p.setBrush(QColor(ui::colors::TEXT_PRIMARY()));
            p.drawPolygon(tri);
            // Beyond-range hint.
            if (high_ > low_ && (price_ < low_ || price_ > high_)) {
                p.setPen(QColor(ui::colors::WARNING()));
                const QString hint = price_ < low_ ? QStringLiteral("▼") : QStringLiteral("▲");
                p.drawText(QPointF(xp - (fm.horizontalAdvance(hint) / 2.0), y_track - 14), hint);
            }
        }
    }

  private:
    double low_ = 0.0;
    double mean_ = 0.0;
    double high_ = 0.0;
    double price_ = 0.0;
    QString cur_ = QStringLiteral("$");
    bool has_data_ = false;
};

// ── Panel helper ───────────────────────────────────────────────────────────────
// Class method (not a free function) so it can register the title label with the
// per-instance i18n map. retranslateUi() iterates the map and re-applies tr().

QFrame* EquityAnalysisTab::make_panel_(const char* title_key, const QString& accent_color) {
    auto* f = new QFrame;
    f->setStyleSheet(QString("QFrame { background:%1; border:1px solid %2; border-radius:4px; }")
                         .arg(ui::colors::BG_SURFACE(), ui::colors::BORDER_DIM()));
    auto* vl = new QVBoxLayout(f);
    vl->setContentsMargins(18, 16, 18, 18);
    vl->setSpacing(12);

    auto* hdr = new QWidget(nullptr);
    hdr->setStyleSheet(QString("background:transparent; border:0; border-bottom:2px solid %1;").arg(accent_color));
    auto* hl = new QHBoxLayout(hdr);
    hl->setContentsMargins(0, 0, 0, 8);
    hl->setSpacing(8);

    auto* bar = new QFrame;
    bar->setFixedSize(4, 16);
    bar->setStyleSheet(QString("background:%1; border:0; border-radius:0;").arg(accent_color));
    hl->addWidget(bar);

    auto* lbl = new QLabel(tr(title_key));
    lbl->setStyleSheet(QString("color:%1; font-size:13px; font-weight:700; letter-spacing:1px; "
                               "background:transparent; border:0;")
                           .arg(accent_color));
    hl->addWidget(lbl);
    hl->addStretch();
    i18n_labels_.insert(lbl, title_key);

    vl->addWidget(hdr);
    return f;
}

// ── EquityAnalysisTab ───────────────────────────────────────────────────────────

EquityAnalysisTab::EquityAnalysisTab(QWidget* parent) : QWidget(parent) {
    build_ui();
    auto& svc = services::equity::EquityResearchService::instance();
    connect(&svc, &services::equity::EquityResearchService::info_loaded, this, &EquityAnalysisTab::on_info_loaded);
    // The overlay was only ever hidden on the success path, so a failed "Info"
    // fetch left "LOADING ANALYSIS…" spinning over the tab forever.
    connect(&svc, &services::equity::EquityResearchService::error_occurred, this,
            [this](const QString& ctx, const QString&) {
                if (ctx == QLatin1String("Info") && loading_overlay_)
                    loading_overlay_->hide_loading();
            });
}

void EquityAnalysisTab::set_symbol(const QString& symbol) {
    if (symbol == current_symbol_)
        return;
    current_symbol_ = symbol;
    info_loaded_ = false;
    cached_info_ = {};
    loading_overlay_->show_loading(tr("LOADING ANALYSIS…"));
    if (kr_panel_)
        kr_panel_->setVisible(is_korean_symbol_());
    if (kr_status_)
        kr_status_->setText(tr("On-demand · research_only · no live order submission"));
    if (kr_result_)
        kr_result_->clear();
    if (kr_research_btn_)
        kr_research_btn_->setEnabled(!kr_market_().isEmpty());
}

void EquityAnalysisTab::build_ui() {
    setStyleSheet(QString("background:%1;").arg(ui::colors::BG_BASE()));
    loading_overlay_ = new ui::LoadingOverlay(this);

    auto* scroll = new QScrollArea;
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setStyleSheet("background:transparent; border:0;");

    auto* content = new QWidget(this);
    auto* root = new QVBoxLayout(content);
    root->setContentsMargins(12, 12, 12, 12);
    root->setSpacing(10);

    // Hero (full width).
    root->addWidget(build_hero_());

    // Verdict grid: 3 columns × 2 rows.
    auto* grid_host = new QWidget(nullptr);
    grid_host->setStyleSheet("background:transparent;");
    auto* grid = new QGridLayout(grid_host);
    grid->setContentsMargins(0, 0, 0, 0);
    grid->setSpacing(10);

    struct DimSpec {
        Dim dim;
        const char* title;
        QString accent;
    };
    const DimSpec specs[kDimCount] = {
        {kValuation, QT_TR_NOOP("VALUATION"), ui::colors::CYAN()},
        {kHealth, QT_TR_NOOP("FINANCIAL HEALTH"), ui::colors::AMBER()},
        {kCashFlow, QT_TR_NOOP("CASH FLOW"), ui::colors::INFO()},
        {kProfitability, QT_TR_NOOP("PROFITABILITY"), ui::colors::POSITIVE()},
        {kGrowth, QT_TR_NOOP("GROWTH"), QStringLiteral("#a855f7")},
        {kRisk, QT_TR_NOOP("RISK / SENTIMENT"), ui::colors::WARNING()},
    };

    for (int i = 0; i < kDimCount; ++i) {
        auto* panel = make_panel_(specs[i].title, specs[i].accent);
        cards_[specs[i].dim] = build_verdict_card_(panel, specs[i].title, specs[i].accent);
        grid->addWidget(panel, i / 3, i % 3);
    }
    grid->setColumnStretch(0, 1);
    grid->setColumnStretch(1, 1);
    grid->setColumnStretch(2, 1);
    grid->setRowStretch(0, 1);
    grid->setRowStretch(1, 1);

    root->addWidget(grid_host, 1);

    // Whole-market discovery is independent of the symbol currently loaded in
    // Equity Research. It is a lightweight, keyless prefilter: no LLM, no paper
    // trade and no live-order path is invoked by this panel.
    kr_discovery_panel_ = build_kr_discovery_panel_();
    root->addWidget(kr_discovery_panel_);

    kr_history_panel_ = build_kr_history_panel_();
    root->addWidget(kr_history_panel_);

    // Korean-market AI research is an explicit, on-demand action. Keeping it
    // inside the existing Analysis tab avoids adding a parallel screen while
    // still giving users a visible path to the new KIS/DART/Naver/ECOS engine.
    kr_panel_ = build_kr_research_panel_();
    kr_panel_->setVisible(is_korean_symbol_());
    root->addWidget(kr_panel_);

    scroll->setWidget(content);
    auto* ol = new QVBoxLayout(this);
    ol->setContentsMargins(0, 0, 0, 0);
    ol->addWidget(scroll);
}

QFrame* EquityAnalysisTab::build_kr_discovery_panel_() {
    auto* panel = make_panel_(QT_TR_NOOP("KR MARKET DISCOVERY"), ui::colors::AMBER());
    auto* vl = static_cast<QVBoxLayout*>(panel->layout());

    auto* description = new QLabel(
        tr("PIT-safe KOSPI/KOSDAQ Top-N discovery. Uses the keyless KIS public master and, when configured, "
           "current KIS trading-value rank. Discovery is research-only and does not run the LLM or submit orders."));
    description->setWordWrap(true);
    vl->addWidget(description);

    auto* controls = new QWidget(nullptr);
    auto* hl = new QHBoxLayout(controls);
    hl->setContentsMargins(0, 0, 0, 0);
    hl->setSpacing(10);

    auto* limit_label = new QLabel(tr("Top N"));
    hl->addWidget(limit_label);
    kr_discovery_limit_ = new QSpinBox;
    kr_discovery_limit_->setRange(1, 50);
    kr_discovery_limit_->setValue(20);
    hl->addWidget(kr_discovery_limit_);

    auto* date_label = new QLabel(tr("Date"));
    hl->addWidget(date_label);
    kr_discovery_date_ = new QDateEdit;
    const QDate korea_today = QDateTime::currentDateTimeUtc().toOffsetFromUtc(9 * 60 * 60).date();
    kr_discovery_date_->setDate(korea_today);
    kr_discovery_date_->setMaximumDate(korea_today);
    kr_discovery_date_->setCalendarPopup(true);
    kr_discovery_date_->setDisplayFormat("yyyy-MM-dd");
    kr_discovery_date_->setToolTip(tr("Past dates require an exact PIT universe snapshot captured on that date"));
    hl->addWidget(kr_discovery_date_);

    auto* profile_label = new QLabel(tr("Profile"));
    hl->addWidget(profile_label);
    kr_discovery_profile_ = new QComboBox;
    kr_discovery_profile_->addItem(tr("Balanced"), "balanced");
    kr_discovery_profile_->addItem(tr("Liquidity"), "liquidity");
    kr_discovery_profile_->addItem(tr("Large Cap"), "large_cap");
    kr_discovery_profile_->addItem(tr("Active"), "active");
    hl->addWidget(kr_discovery_profile_);

    auto* market_label = new QLabel(tr("Market"));
    hl->addWidget(market_label);
    kr_discovery_market_ = new QComboBox;
    kr_discovery_market_->addItem(tr("All"), "ALL");
    kr_discovery_market_->addItem(tr("KOSPI"), "KOSPI");
    kr_discovery_market_->addItem(tr("KOSDAQ"), "KOSDAQ");
    hl->addWidget(kr_discovery_market_);

    auto* min_value_label = new QLabel(tr("Min trading value"));
    hl->addWidget(min_value_label);
    kr_discovery_min_value_ = new QDoubleSpinBox;
    kr_discovery_min_value_->setDecimals(0);
    kr_discovery_min_value_->setRange(0, 100000);
    kr_discovery_min_value_->setSingleStep(10);
    kr_discovery_min_value_->setSuffix(tr(" 억"));
    kr_discovery_min_value_->setToolTip(tr("Minimum trading-value filter in KRW 100 million units"));
    hl->addWidget(kr_discovery_min_value_);

    kr_discover_btn_ = new QPushButton(tr("DISCOVER KR TOP-N"));
    kr_discover_btn_->setCursor(Qt::PointingHandCursor);
    connect(kr_discover_btn_, &QPushButton::clicked, this, &EquityAnalysisTab::on_kr_discover_clicked);
    hl->addWidget(kr_discover_btn_);

    kr_discovery_research_btn_ = new QPushButton(tr("RESEARCH SELECTED"));
    kr_discovery_research_btn_->setCursor(Qt::PointingHandCursor);
    kr_discovery_research_btn_->setEnabled(false);
    connect(kr_discovery_research_btn_, &QPushButton::clicked, this,
            &EquityAnalysisTab::on_kr_discovery_research_clicked);
    hl->addWidget(kr_discovery_research_btn_);

    kr_discovery_batch_btn_ = new QPushButton(tr("RESEARCH TOP-N (MAX 10)"));
    kr_discovery_batch_btn_->setCursor(Qt::PointingHandCursor);
    kr_discovery_batch_btn_->setEnabled(false);
    connect(kr_discovery_batch_btn_, &QPushButton::clicked, this,
            &EquityAnalysisTab::on_kr_discovery_batch_clicked);
    hl->addWidget(kr_discovery_batch_btn_);

    kr_discovery_status_ = new QLabel(tr("Keyless current discovery · exact PIT snapshot replay · research_only"));
    hl->addWidget(kr_discovery_status_, 1);
    vl->addWidget(controls);

    kr_discovery_table_ = new QTableWidget(0, 8);
    kr_discovery_table_->setHorizontalHeaderLabels(
        {tr("Rank"), tr("Ticker"), tr("Company"), tr("Market"), tr("Score"),
         tr("Liquidity"), tr("Size"), tr("Turnover")});
    kr_discovery_table_->horizontalHeader()->setSectionResizeMode(QHeaderView::ResizeToContents);
    kr_discovery_table_->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Stretch);
    kr_discovery_table_->setEditTriggers(QAbstractItemView::NoEditTriggers);
    kr_discovery_table_->setSelectionBehavior(QAbstractItemView::SelectRows);
    kr_discovery_table_->setAlternatingRowColors(true);
    kr_discovery_table_->setMinimumHeight(260);
    vl->addWidget(kr_discovery_table_);

    kr_discovery_result_ = new QPlainTextEdit;
    kr_discovery_result_->setReadOnly(true);
    kr_discovery_result_->setPlaceholderText(
        tr("Select a discovered stock and press RESEARCH SELECTED to run one explicit research_only deep analysis."));
    kr_discovery_result_->setMinimumHeight(180);
    vl->addWidget(kr_discovery_result_);
    return panel;
}

QFrame* EquityAnalysisTab::build_kr_history_panel_() {
    auto* panel = make_panel_(QT_TR_NOOP("KR RESEARCH HISTORY"), ui::colors::INFO());
    auto* vl = static_cast<QVBoxLayout*>(panel->layout());

    auto* description = new QLabel(
        tr("Frozen Personal-KR decisions, forward outcome/benchmark-alpha evaluation, and paper portfolio summary. "
           "Evaluation is explicit and paper data never routes to live brokerage."));
    description->setWordWrap(true);
    vl->addWidget(description);

    auto* controls = new QWidget(nullptr);
    auto* hl = new QHBoxLayout(controls);
    hl->setContentsMargins(0, 0, 0, 0);
    hl->setSpacing(10);

    kr_history_refresh_btn_ = new QPushButton(tr("REFRESH DECISIONS"));
    connect(kr_history_refresh_btn_, &QPushButton::clicked, this, &EquityAnalysisTab::on_kr_history_refresh_clicked);
    hl->addWidget(kr_history_refresh_btn_);

    kr_history_evaluate_btn_ = new QPushButton(tr("EVALUATE 1/5/20/60D"));
    kr_history_evaluate_btn_->setEnabled(false);
    connect(kr_history_evaluate_btn_, &QPushButton::clicked, this, &EquityAnalysisTab::on_kr_history_evaluate_clicked);
    hl->addWidget(kr_history_evaluate_btn_);

    kr_history_outcomes_btn_ = new QPushButton(tr("SHOW OUTCOMES"));
    kr_history_outcomes_btn_->setEnabled(false);
    connect(kr_history_outcomes_btn_, &QPushButton::clicked, this, &EquityAnalysisTab::on_kr_history_outcomes_clicked);
    hl->addWidget(kr_history_outcomes_btn_);

    kr_history_paper_btn_ = new QPushButton(tr("PAPER SUMMARY"));
    connect(kr_history_paper_btn_, &QPushButton::clicked, this,
            &EquityAnalysisTab::on_kr_history_paper_summary_clicked);
    hl->addWidget(kr_history_paper_btn_);

    kr_history_paper_trades_btn_ = new QPushButton(tr("PAPER TRADES"));
    connect(kr_history_paper_trades_btn_, &QPushButton::clicked, this,
            &EquityAnalysisTab::on_kr_history_paper_trades_clicked);
    hl->addWidget(kr_history_paper_trades_btn_);

    kr_history_status_ = new QLabel(tr("Frozen decisions · benchmark alpha · paper_only summary"));
    hl->addWidget(kr_history_status_, 1);
    vl->addWidget(controls);

    kr_history_table_ = new QTableWidget(0, 7);
    kr_history_table_->setHorizontalHeaderLabels(
        {tr("Date"), tr("Ticker"), tr("Company"), tr("Signal"), tr("Score"), tr("Strategy"), tr("Decision ID")});
    kr_history_table_->horizontalHeader()->setSectionResizeMode(QHeaderView::ResizeToContents);
    kr_history_table_->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Stretch);
    kr_history_table_->horizontalHeader()->setSectionResizeMode(6, QHeaderView::Stretch);
    kr_history_table_->setEditTriggers(QAbstractItemView::NoEditTriggers);
    kr_history_table_->setSelectionBehavior(QAbstractItemView::SelectRows);
    kr_history_table_->setSelectionMode(QAbstractItemView::SingleSelection);
    kr_history_table_->setMinimumHeight(220);
    connect(kr_history_table_, &QTableWidget::itemSelectionChanged, this, [this]() {
        kr_history_pending_paper_trade_ = {};
        const bool selected = !selected_kr_decision_id_().isEmpty();
        if (kr_history_evaluate_btn_)
            kr_history_evaluate_btn_->setEnabled(selected && !kr_history_busy_);
        if (kr_history_outcomes_btn_)
            kr_history_outcomes_btn_->setEnabled(selected && !kr_history_busy_);
        if (kr_history_paper_trade_btn_)
            kr_history_paper_trade_btn_->setEnabled(selected && !kr_history_busy_);
    });
    vl->addWidget(kr_history_table_);

    auto* paper_controls = new QWidget(nullptr);
    auto* paper_hl = new QHBoxLayout(paper_controls);
    paper_hl->setContentsMargins(0, 0, 0, 0);
    paper_hl->setSpacing(8);

    auto* paper_label = new QLabel(tr("Manual paper trade:"));
    paper_hl->addWidget(paper_label);

    kr_history_paper_side_ = new QComboBox;
    kr_history_paper_side_->addItem(QStringLiteral("BUY"), QStringLiteral("BUY"));
    kr_history_paper_side_->addItem(QStringLiteral("SELL"), QStringLiteral("SELL"));
    paper_hl->addWidget(kr_history_paper_side_);

    kr_history_paper_quantity_ = new QSpinBox;
    kr_history_paper_quantity_->setRange(1, 100000000);
    kr_history_paper_quantity_->setValue(1);
    kr_history_paper_quantity_->setPrefix(tr("Qty "));
    paper_hl->addWidget(kr_history_paper_quantity_);

    kr_history_paper_price_ = new QDoubleSpinBox;
    kr_history_paper_price_->setRange(0.0, 1000000000000.0);
    kr_history_paper_price_->setDecimals(2);
    kr_history_paper_price_->setSingleStep(100.0);
    kr_history_paper_price_->setPrefix(tr("Price ₩"));
    kr_history_paper_price_->setSpecialValueText(tr("Price required"));
    paper_hl->addWidget(kr_history_paper_price_);

    kr_history_paper_fee_ = new QDoubleSpinBox;
    kr_history_paper_fee_->setRange(0.0, 1000000000.0);
    kr_history_paper_fee_->setDecimals(2);
    kr_history_paper_fee_->setPrefix(tr("Fee ₩"));
    paper_hl->addWidget(kr_history_paper_fee_);

    kr_history_paper_tax_ = new QDoubleSpinBox;
    kr_history_paper_tax_->setRange(0.0, 1000000000.0);
    kr_history_paper_tax_->setDecimals(2);
    kr_history_paper_tax_->setPrefix(tr("Tax ₩"));
    paper_hl->addWidget(kr_history_paper_tax_);

    kr_history_paper_trade_btn_ = new QPushButton(tr("RECORD PAPER TRADE"));
    kr_history_paper_trade_btn_->setEnabled(false);
    connect(kr_history_paper_trade_btn_, &QPushButton::clicked, this,
            &EquityAnalysisTab::on_kr_history_paper_trade_clicked);
    paper_hl->addWidget(kr_history_paper_trade_btn_);
    paper_hl->addStretch(1);
    vl->addWidget(paper_controls);

    kr_history_result_ = new QPlainTextEdit;
    kr_history_result_->setReadOnly(true);
    kr_history_result_->setPlaceholderText(
        tr("Refresh decisions, select a row, then evaluate or inspect frozen outcomes. Paper summary is read-only here."));
    kr_history_result_->setMinimumHeight(180);
    vl->addWidget(kr_history_result_);
    return panel;
}

QString EquityAnalysisTab::selected_kr_decision_id_() const {
    if (!kr_history_table_)
        return {};
    const int row = kr_history_table_->currentRow();
    if (row < 0 || row >= kr_history_decisions_.size())
        return {};
    return kr_history_decisions_.at(row).toObject().value("decision_id").toString();
}

void EquityAnalysisTab::set_kr_history_busy_(bool busy) {
    kr_history_busy_ = busy;
    if (kr_history_refresh_btn_)
        kr_history_refresh_btn_->setEnabled(!busy);
    if (kr_history_paper_btn_)
        kr_history_paper_btn_->setEnabled(!busy);
    if (kr_history_paper_trades_btn_)
        kr_history_paper_trades_btn_->setEnabled(!busy);
    const bool selected = !selected_kr_decision_id_().isEmpty();
    if (kr_history_evaluate_btn_)
        kr_history_evaluate_btn_->setEnabled(!busy && selected);
    if (kr_history_outcomes_btn_)
        kr_history_outcomes_btn_->setEnabled(!busy && selected);
    if (kr_history_paper_trade_btn_)
        kr_history_paper_trade_btn_->setEnabled(!busy && selected);
    for (QWidget* control : {static_cast<QWidget*>(kr_history_paper_side_),
                             static_cast<QWidget*>(kr_history_paper_quantity_),
                             static_cast<QWidget*>(kr_history_paper_price_),
                             static_cast<QWidget*>(kr_history_paper_fee_),
                             static_cast<QWidget*>(kr_history_paper_tax_)}) {
        if (control)
            control->setEnabled(!busy);
    }
}

void EquityAnalysisTab::on_kr_history_refresh_clicked() {
    if (!kr_history_table_ || !kr_history_result_ || !kr_history_status_)
        return;
    set_kr_history_busy_(true);
    kr_history_status_->setText(tr("Loading frozen KR decisions…"));

    python::PythonRunner::RunOptions opts;
    opts.timeout_ms = 60 * 1000;
    QPointer<EquityAnalysisTab> self(this);
    python::PythonRunner::instance().run_with_options(
        "personal_kr_terminal.py", {"decisions", "--limit", "50"}, opts,
        [self](python::PythonResult result) {
            if (!self)
                return;
            if (!result.success) {
                self->set_kr_history_busy_(false);
                self->kr_history_status_->setText(self->tr("KR decision history unavailable"));
                self->kr_history_result_->setPlainText(result.error);
                return;
            }
            const QJsonDocument doc = QJsonDocument::fromJson(python::extract_json(result.output).toUtf8());
            if (!doc.isObject() || !doc.object().value("success").toBool(false)) {
                const QString error = doc.isObject() ? doc.object().value("error").toString() : result.output;
                self->set_kr_history_busy_(false);
                self->kr_history_status_->setText(self->tr("KR decision history unavailable"));
                self->kr_history_result_->setPlainText(error);
                return;
            }
            const QJsonArray rows = doc.object().value("data").toArray();
            self->kr_history_decisions_ = rows;
            self->kr_history_table_->setRowCount(rows.size());
            for (int row = 0; row < rows.size(); ++row) {
                const QJsonObject item = rows.at(row).toObject();
                const QJsonObject candidate = item.value("candidate").toObject();
                const QJsonObject instrument = candidate.value("instrument").toObject();
                const QStringList values{
                    candidate.value("analysis_date").toString(),
                    instrument.value("ticker").toString(),
                    instrument.value("name").toString(),
                    item.value("signal").toString(),
                    QString::number(candidate.value("score").toDouble(), 'f', 2),
                    item.value("strategy_id").toString(),
                    item.value("decision_id").toString(),
                };
                for (int col = 0; col < values.size(); ++col)
                    self->kr_history_table_->setItem(row, col, new QTableWidgetItem(values.at(col)));
            }
            if (!rows.isEmpty())
                self->kr_history_table_->selectRow(0);
            self->set_kr_history_busy_(false);
            self->kr_history_status_->setText(self->tr("Loaded %1 frozen decision(s)").arg(rows.size()));
            self->kr_history_result_->clear();
        });
}

void EquityAnalysisTab::on_kr_history_evaluate_clicked() {
    const QString decision_id = selected_kr_decision_id_();
    if (decision_id.isEmpty() || !kr_history_result_ || !kr_history_status_)
        return;
    set_kr_history_busy_(true);
    kr_history_status_->setText(tr("Evaluating 1/5/20/60-session outcomes…"));

    python::PythonRunner::RunOptions opts;
    opts.timeout_ms = 10 * 60 * 1000;
    QPointer<EquityAnalysisTab> self(this);
    python::PythonRunner::instance().run_with_options(
        "personal_kr_terminal.py", {"evaluate", decision_id, "--horizons", "1", "5", "20", "60"}, opts,
        [self](python::PythonResult result) {
            if (!self)
                return;
            self->set_kr_history_busy_(false);
            if (!result.success) {
                self->kr_history_status_->setText(self->tr("KR outcome evaluation unavailable"));
                self->kr_history_result_->setPlainText(result.error);
                return;
            }
            const QJsonDocument doc = QJsonDocument::fromJson(python::extract_json(result.output).toUtf8());
            if (!doc.isObject() || !doc.object().value("success").toBool(false)) {
                const QString error = doc.isObject() ? doc.object().value("error").toString() : result.output;
                self->kr_history_status_->setText(self->tr("KR outcome evaluation unavailable"));
                self->kr_history_result_->setPlainText(error);
                return;
            }
            const QJsonObject data = doc.object().value("data").toObject();
            const int completed = data.value("outcomes").toArray().size();
            const int pending = data.value("pending_horizons").toArray().size();
            self->kr_history_status_->setText(
                self->tr("Outcome evaluation · %1 frozen · %2 pending").arg(completed).arg(pending));
            self->kr_history_result_->setPlainText(
                QString::fromUtf8(QJsonDocument(data).toJson(QJsonDocument::Indented)));
        });
}

void EquityAnalysisTab::on_kr_history_outcomes_clicked() {
    const QString decision_id = selected_kr_decision_id_();
    if (decision_id.isEmpty() || !kr_history_result_ || !kr_history_status_)
        return;
    set_kr_history_busy_(true);
    kr_history_status_->setText(tr("Loading frozen outcomes…"));

    python::PythonRunner::RunOptions opts;
    opts.timeout_ms = 60 * 1000;
    QPointer<EquityAnalysisTab> self(this);
    python::PythonRunner::instance().run_with_options(
        "personal_kr_terminal.py", {"outcomes", decision_id}, opts,
        [self](python::PythonResult result) {
            if (!self)
                return;
            self->set_kr_history_busy_(false);
            if (!result.success) {
                self->kr_history_status_->setText(self->tr("KR outcomes unavailable"));
                self->kr_history_result_->setPlainText(result.error);
                return;
            }
            const QJsonDocument doc = QJsonDocument::fromJson(python::extract_json(result.output).toUtf8());
            if (!doc.isObject() || !doc.object().value("success").toBool(false)) {
                const QString error = doc.isObject() ? doc.object().value("error").toString() : result.output;
                self->kr_history_status_->setText(self->tr("KR outcomes unavailable"));
                self->kr_history_result_->setPlainText(error);
                return;
            }
            const QJsonArray data = doc.object().value("data").toArray();
            self->kr_history_status_->setText(self->tr("Loaded %1 frozen outcome(s)").arg(data.size()));
            self->kr_history_result_->setPlainText(
                QString::fromUtf8(QJsonDocument(data).toJson(QJsonDocument::Indented)));
        });
}

void EquityAnalysisTab::on_kr_history_paper_summary_clicked() {
    if (!kr_history_result_ || !kr_history_status_)
        return;
    set_kr_history_busy_(true);
    kr_history_status_->setText(tr("Loading Personal-KR paper portfolio summary…"));

    python::PythonRunner::RunOptions opts;
    opts.timeout_ms = 60 * 1000;
    QPointer<EquityAnalysisTab> self(this);
    python::PythonRunner::instance().run_with_options(
        "personal_kr_terminal.py", {"paper-summary"}, opts,
        [self](python::PythonResult result) {
            if (!self)
                return;
            self->set_kr_history_busy_(false);
            if (!result.success) {
                self->kr_history_status_->setText(self->tr("Paper summary unavailable"));
                self->kr_history_result_->setPlainText(result.error);
                return;
            }
            const QJsonDocument doc = QJsonDocument::fromJson(python::extract_json(result.output).toUtf8());
            if (!doc.isObject() || !doc.object().value("success").toBool(false)) {
                const QString error = doc.isObject() ? doc.object().value("error").toString() : result.output;
                self->kr_history_status_->setText(self->tr("Paper summary unavailable"));
                self->kr_history_result_->setPlainText(error);
                return;
            }
            const QJsonObject data = doc.object().value("data").toObject();
            if (data.value("execution_mode").toString() != QLatin1String("paper_only")) {
                self->kr_history_status_->setText(self->tr("Unexpected paper summary execution mode"));
                return;
            }
            self->kr_history_status_->setText(self->tr("Personal-KR paper portfolio · paper_only"));
            self->kr_history_result_->setPlainText(
                QString::fromUtf8(QJsonDocument(data).toJson(QJsonDocument::Indented)));
        });
}

void EquityAnalysisTab::on_kr_history_paper_trades_clicked() {
    if (!kr_history_result_ || !kr_history_status_)
        return;
    set_kr_history_busy_(true);
    kr_history_status_->setText(tr("Loading Personal-KR paper ledger…"));

    python::PythonRunner::RunOptions opts;
    opts.timeout_ms = 60 * 1000;
    QPointer<EquityAnalysisTab> self(this);
    python::PythonRunner::instance().run_with_options(
        "personal_kr_terminal.py", {"paper-trades", "--limit", "100"}, opts,
        [self](python::PythonResult result) {
            if (!self)
                return;
            self->set_kr_history_busy_(false);
            if (!result.success) {
                self->kr_history_status_->setText(self->tr("Paper ledger unavailable"));
                self->kr_history_result_->setPlainText(result.error);
                return;
            }
            const QJsonDocument doc = QJsonDocument::fromJson(python::extract_json(result.output).toUtf8());
            if (!doc.isObject() || !doc.object().value("success").toBool(false)) {
                const QString error = doc.isObject() ? doc.object().value("error").toString() : result.output;
                self->kr_history_status_->setText(self->tr("Paper ledger unavailable"));
                self->kr_history_result_->setPlainText(error);
                return;
            }
            const QJsonObject data = doc.object().value("data").toObject();
            if (data.value("execution_mode").toString() != QLatin1String("paper_only")) {
                self->kr_history_status_->setText(self->tr("Unexpected paper ledger execution mode"));
                self->kr_history_result_->setPlainText(result.output);
                return;
            }
            const QJsonArray trades = data.value("trades").toArray();
            self->kr_history_status_->setText(self->tr("Loaded %1 paper-only trade(s)").arg(trades.size()));
            self->kr_history_result_->setPlainText(
                QString::fromUtf8(QJsonDocument(trades).toJson(QJsonDocument::Indented)));
        });
}

void EquityAnalysisTab::on_kr_history_paper_trade_clicked() {
    const QString decision_id = selected_kr_decision_id_();
    const int row = kr_history_table_ ? kr_history_table_->currentRow() : -1;
    if (decision_id.isEmpty() || row < 0 || row >= kr_history_decisions_.size() || !kr_history_paper_side_ ||
        !kr_history_paper_quantity_ || !kr_history_paper_price_ || !kr_history_paper_fee_ ||
        !kr_history_paper_tax_ || !kr_history_result_ || !kr_history_status_)
        return;

    const QJsonObject decision = kr_history_decisions_.at(row).toObject();
    const QString ticker = decision.value("candidate").toObject().value("instrument").toObject().value("ticker").toString();
    const QString side = kr_history_paper_side_->currentData().toString();
    const int quantity = kr_history_paper_quantity_->value();
    const double price = kr_history_paper_price_->value();
    const double fee = kr_history_paper_fee_->value();
    const double tax = kr_history_paper_tax_->value();
    if (ticker.isEmpty() || quantity <= 0 || price <= 0.0) {
        kr_history_status_->setText(tr("Select a frozen decision and enter a positive paper execution price."));
        return;
    }

    const QString trade_date = QDateTime::currentDateTimeUtc().toOffsetFromUtc(9 * 60 * 60).date().toString(Qt::ISODate);
    QJsonObject base_payload{
        {"decision_id", decision_id},
        {"trade_date", trade_date},
        {"ticker", ticker},
        {"side", side},
        {"quantity", quantity},
        {"price", price},
        {"fee", fee},
        {"tax", tax},
    };

    QJsonObject payload = base_payload;
    bool retrying = false;
    if (!kr_history_pending_paper_trade_.isEmpty()) {
        QJsonObject pending_base = kr_history_pending_paper_trade_;
        pending_base.remove("client_trade_id");
        if (pending_base != base_payload) {
            QMessageBox::warning(
                this, tr("Pending paper-only request"),
                tr("A previous paper request did not return a definitive process result. Retry the same values first so "
                   "the existing idempotency key can prevent an accidental duplicate. PAPER SUMMARY can be used to inspect "
                   "the ledger before retrying."));
            return;
        }
        payload = kr_history_pending_paper_trade_;
        retrying = true;
    } else {
        payload["client_trade_id"] = QStringLiteral("qt-") + QUuid::createUuid().toString(QUuid::WithoutBraces);
    }

    const QString confirmation =
        tr("%1 simulated %2 · %3 share(s) @ ₩%4\nFee ₩%5 · Tax ₩%6\n\n"
           "This writes only to the isolated Personal-KR paper ledger. No live brokerage order will be sent.%7")
            .arg(trade_date)
            .arg(side)
            .arg(quantity)
            .arg(QString::number(price, 'f', 2))
            .arg(QString::number(fee, 'f', 2))
            .arg(QString::number(tax, 'f', 2))
            .arg(retrying ? tr("\n\nThis retry will reuse the same idempotency key.") : QString());
    if (QMessageBox::question(this, tr("Confirm paper-only trade"), confirmation, QMessageBox::Yes | QMessageBox::No,
                              QMessageBox::No) != QMessageBox::Yes)
        return;

    if (!retrying)
        kr_history_pending_paper_trade_ = payload;

    set_kr_history_busy_(true);
    kr_history_status_->setText(retrying ? tr("Retrying idempotent paper-only request…")
                                         : tr("Recording paper-only trade…"));
    python::PythonRunner::RunOptions opts;
    opts.timeout_ms = 60 * 1000;
    opts.stdin_data = QJsonDocument(payload).toJson(QJsonDocument::Compact);
    QPointer<EquityAnalysisTab> self(this);
    python::PythonRunner::instance().run_with_options(
        "personal_kr_terminal.py", {"paper-trade"}, opts,
        [self](python::PythonResult result) {
            if (!self)
                return;
            self->set_kr_history_busy_(false);
            if (!result.success) {
                self->kr_history_status_->setText(
                    self->tr("Paper trade process did not confirm completion; retry the same values to reuse its idempotency key."));
                self->kr_history_result_->setPlainText(result.error);
                return;
            }

            const QJsonDocument doc = QJsonDocument::fromJson(python::extract_json(result.output).toUtf8());
            if (!doc.isObject()) {
                self->kr_history_status_->setText(
                    self->tr("Paper trade returned an ambiguous response; retry the same values before starting a new request."));
                self->kr_history_result_->setPlainText(result.output);
                return;
            }
            if (!doc.object().value("success").toBool(false)) {
                self->kr_history_pending_paper_trade_ = {};
                self->kr_history_status_->setText(self->tr("Paper trade rejected"));
                self->kr_history_result_->setPlainText(doc.object().value("error").toString());
                return;
            }
            const QJsonObject data = doc.object().value("data").toObject();
            if (data.value("execution_mode").toString() != QLatin1String("paper_only")) {
                self->kr_history_status_->setText(
                    self->tr("Unexpected paper execution mode; retry the same values only after inspecting PAPER SUMMARY."));
                self->kr_history_result_->setPlainText(result.output);
                return;
            }
            self->kr_history_pending_paper_trade_ = {};
            self->kr_history_status_->setText(
                self->tr("Recorded paper-only trade #%1 · no live order").arg(data.value("trade_id").toInt()));
            self->kr_history_result_->setPlainText(
                QString::fromUtf8(QJsonDocument(data).toJson(QJsonDocument::Indented)));
        });
}

void EquityAnalysisTab::on_kr_discover_clicked() {
    if (!kr_discover_btn_ || !kr_discovery_table_ || !kr_discovery_limit_)
        return;

    const int limit = kr_discovery_limit_->value();
    const QDate korea_today = QDateTime::currentDateTimeUtc().toOffsetFromUtc(9 * 60 * 60).date();
    if (kr_discovery_date_)
        kr_discovery_date_->setMaximumDate(korea_today);
    const QDate analysis_date = kr_discovery_date_ ? kr_discovery_date_->date() : korea_today;
    const QString profile = kr_discovery_profile_ ? kr_discovery_profile_->currentData().toString()
                                                  : QStringLiteral("balanced");
    const QString market = kr_discovery_market_ ? kr_discovery_market_->currentData().toString()
                                                : QStringLiteral("ALL");
    const qint64 min_trading_value_krw = kr_discovery_min_value_
                                             ? static_cast<qint64>(kr_discovery_min_value_->value() * 100000000.0)
                                             : 0;
    kr_discover_btn_->setEnabled(false);
    if (kr_discovery_research_btn_)
        kr_discovery_research_btn_->setEnabled(false);
    if (kr_discovery_batch_btn_)
        kr_discovery_batch_btn_->setEnabled(false);
    kr_discovery_status_->setText(tr("Discovering current KOSPI/KOSDAQ universe…"));
    kr_discovery_table_->setRowCount(0);
    kr_discovery_candidates_ = {};
    kr_discovery_ranking_ = {};
    if (kr_discovery_result_)
        kr_discovery_result_->clear();

    python::PythonRunner::RunOptions run_opts;
    run_opts.timeout_ms = 5 * 60 * 1000;
    QPointer<EquityAnalysisTab> self(this);
    QStringList script_args{
        "discover", "--limit", QString::number(limit),
        "--analysis-date", analysis_date.toString(Qt::ISODate),
        "--profile", profile,
        "--min-trading-value-krw", QString::number(min_trading_value_krw),
    };
    if (market != QLatin1String("ALL"))
        script_args << "--market" << market;
    python::PythonRunner::instance().run_with_options(
        "personal_kr_terminal.py", script_args, run_opts,
        [self](python::PythonResult result) {
            if (!self)
                return;
            if (self->kr_discover_btn_)
                self->kr_discover_btn_->setEnabled(true);
            if (!result.success) {
                if (self->kr_discovery_status_)
                    self->kr_discovery_status_->setText(self->tr("KR market discovery unavailable: %1").arg(result.error));
                return;
            }

            const QJsonDocument doc = QJsonDocument::fromJson(python::extract_json(result.output).toUtf8());
            if (!doc.isObject() || !doc.object().value("success").toBool(false)) {
                const QString error = doc.isObject() ? doc.object().value("error").toString() : result.output;
                if (self->kr_discovery_status_)
                    self->kr_discovery_status_->setText(self->tr("KR market discovery unavailable: %1").arg(error));
                return;
            }

            const QJsonObject data = doc.object().value("data").toObject();
            const QJsonArray candidates = data.value("candidates").toArray();
            self->kr_discovery_candidates_ = candidates;
            self->kr_discovery_ranking_ = data.value("ranking").toObject();
            self->kr_discovery_table_->setRowCount(candidates.size());
            for (int row = 0; row < candidates.size(); ++row) {
                const QJsonObject candidate = candidates.at(row).toObject();
                const QJsonObject instrument = candidate.value("instrument").toObject();
                const QJsonObject factors = candidate.value("factors").toObject();
                const QStringList values{
                    QString::number(candidate.value("rank").toInt(row + 1)),
                    instrument.value("ticker").toString(),
                    instrument.value("name").toString(),
                    instrument.value("market").toString(),
                    QString::number(candidate.value("score").toDouble(), 'f', 2),
                    QString::number(factors.value("liquidity_score").toDouble(), 'f', 1),
                    QString::number(factors.value("size_score").toDouble(), 'f', 1),
                    QString::number(factors.value("turnover_score").toDouble(), 'f', 1),
                };
                for (int col = 0; col < values.size(); ++col)
                    self->kr_discovery_table_->setItem(row, col, new QTableWidgetItem(values.at(col)));
            }
            if (!candidates.isEmpty()) {
                self->kr_discovery_table_->selectRow(0);
                if (self->kr_discovery_research_btn_)
                    self->kr_discovery_research_btn_->setEnabled(true);
                if (self->kr_discovery_batch_btn_ && !self->kr_discovery_ranking_.isEmpty())
                    self->kr_discovery_batch_btn_->setEnabled(true);
            }

            if (self->kr_discovery_status_) {
                const int universe_count = data.value("snapshot_entry_count").toInt();
                const QString hash = data.value("snapshot_hash").toString().left(12);
                const QString profile = data.value("scoring_profile").toString("balanced");
                const int overlay_count = data.value("rank_overlay_count").toInt();
                const QString analysis_date = data.value("analysis_date").toString();
                self->kr_discovery_status_->setText(
                    self->tr("Completed · %1 · %2 · %3 eligible · Top %4 · KIS overlay %5 · snapshot %6 · research_only")
                        .arg(analysis_date)
                        .arg(profile)
                        .arg(universe_count)
                        .arg(candidates.size())
                        .arg(overlay_count)
                        .arg(hash));
            }
        });
}

void EquityAnalysisTab::on_kr_discovery_research_clicked() {
    if (!kr_discovery_table_ || !kr_discovery_research_btn_ || !kr_discovery_result_)
        return;
    const int row = kr_discovery_table_->currentRow();
    if (row < 0 || row >= kr_discovery_candidates_.size()) {
        kr_discovery_status_->setText(tr("Select one discovered stock first."));
        return;
    }

    QJsonObject payload = kr_discovery_candidates_.at(row).toObject();
    if (payload.isEmpty()) {
        kr_discovery_status_->setText(tr("Selected discovery row has no research payload."));
        return;
    }
    payload["strategy_id"] = "personal-kr-discovery-ui";
    const QJsonObject llm = fincept::services::equity::personal_kr_active_llm_config();
    if (!llm.isEmpty())
        payload["llm"] = llm;

    const QJsonObject instrument = payload.value("instrument").toObject();
    const QString ticker = instrument.value("ticker").toString();
    const QString name = instrument.value("name").toString();
    kr_discovery_research_btn_->setEnabled(false);
    kr_discover_btn_->setEnabled(false);
    kr_discovery_status_->setText(tr("Running selected KR AI research: %1 %2…").arg(ticker, name));
    kr_discovery_result_->setPlainText(tr("Collecting point-in-time provider data and running the research chain…"));

    python::PythonRunner::RunOptions run_opts;
    run_opts.timeout_ms = 20 * 60 * 1000;
    run_opts.stdin_data = QJsonDocument(payload).toJson(QJsonDocument::Compact);
    QPointer<EquityAnalysisTab> self(this);
    python::PythonRunner::instance().run_with_options(
        "personal_kr_terminal.py", {"analyze"}, run_opts,
        [self, ticker, name](python::PythonResult result) {
            if (!self)
                return;
            if (self->kr_discover_btn_)
                self->kr_discover_btn_->setEnabled(true);
            if (self->kr_discovery_research_btn_)
                self->kr_discovery_research_btn_->setEnabled(!self->kr_discovery_candidates_.isEmpty());
            if (!result.success) {
                if (self->kr_discovery_status_)
                    self->kr_discovery_status_->setText(self->tr("Selected KR AI research unavailable"));
                if (self->kr_discovery_result_)
                    self->kr_discovery_result_->setPlainText(result.error);
                return;
            }

            const QJsonDocument doc = QJsonDocument::fromJson(python::extract_json(result.output).toUtf8());
            if (!doc.isObject() || !doc.object().value("success").toBool(false)) {
                const QString error = doc.isObject() ? doc.object().value("error").toString() : result.output;
                if (self->kr_discovery_status_)
                    self->kr_discovery_status_->setText(self->tr("Selected KR AI research unavailable"));
                if (self->kr_discovery_result_)
                    self->kr_discovery_result_->setPlainText(error);
                return;
            }
            const QJsonObject data = doc.object().value("data").toObject();
            const QString signal = data.value("signal").toString();
            if (signal != QLatin1String("Buy") && signal != QLatin1String("Hold") && signal != QLatin1String("Sell")) {
                if (self->kr_discovery_status_)
                    self->kr_discovery_status_->setText(self->tr("Invalid Portfolio Manager signal contract"));
                return;
            }
            if (self->kr_discovery_status_)
                self->kr_discovery_status_->setText(
                    self->tr("Completed · %1 %2 · signal: %3 · research_only").arg(ticker, name, signal));
            if (self->kr_discovery_result_)
                self->kr_discovery_result_->setPlainText(
                    QString::fromUtf8(QJsonDocument(data).toJson(QJsonDocument::Indented)));
        });
}

void EquityAnalysisTab::on_kr_discovery_batch_clicked() {
    if (!kr_discovery_batch_btn_ || !kr_discovery_result_ || kr_discovery_ranking_.isEmpty())
        return;

    QJsonObject payload = kr_discovery_ranking_;
    payload["strategy_id"] = "personal-kr-discovery-batch-ui";
    const QJsonObject llm = fincept::services::equity::personal_kr_active_llm_config();
    if (!llm.isEmpty())
        payload["llm"] = llm;

    const int requested = payload.value("limit").toInt(5);
    kr_discover_btn_->setEnabled(false);
    if (kr_discovery_research_btn_)
        kr_discovery_research_btn_->setEnabled(false);
    kr_discovery_batch_btn_->setEnabled(false);
    kr_discovery_status_->setText(
        tr("Running KR Top-N deep research (max %1)… completed names are checkpointed individually.").arg(requested));
    kr_discovery_result_->setPlainText(
        tr("Running the bounded research_only batch. One candidate failure will not erase completed decisions…"));

    python::PythonRunner::RunOptions run_opts;
    run_opts.timeout_ms = 60 * 60 * 1000;
    run_opts.stdin_data = QJsonDocument(payload).toJson(QJsonDocument::Compact);
    QPointer<EquityAnalysisTab> self(this);
    python::PythonRunner::instance().run_with_options(
        "personal_kr_terminal.py", {"batch"}, run_opts,
        [self](python::PythonResult result) {
            if (!self)
                return;
            if (self->kr_discover_btn_)
                self->kr_discover_btn_->setEnabled(true);
            if (self->kr_discovery_research_btn_)
                self->kr_discovery_research_btn_->setEnabled(!self->kr_discovery_candidates_.isEmpty());
            if (self->kr_discovery_batch_btn_)
                self->kr_discovery_batch_btn_->setEnabled(!self->kr_discovery_ranking_.isEmpty());

            if (!result.success) {
                if (self->kr_discovery_status_)
                    self->kr_discovery_status_->setText(self->tr("KR Top-N research unavailable"));
                if (self->kr_discovery_result_)
                    self->kr_discovery_result_->setPlainText(result.error);
                return;
            }

            const QJsonDocument doc = QJsonDocument::fromJson(python::extract_json(result.output).toUtf8());
            if (!doc.isObject() || !doc.object().value("success").toBool(false)) {
                const QString error = doc.isObject() ? doc.object().value("error").toString() : result.output;
                if (self->kr_discovery_status_)
                    self->kr_discovery_status_->setText(self->tr("KR Top-N research unavailable"));
                if (self->kr_discovery_result_)
                    self->kr_discovery_result_->setPlainText(error);
                return;
            }

            const QJsonObject data = doc.object().value("data").toObject();
            if (data.value("execution_mode").toString() != QLatin1String("research_only")) {
                if (self->kr_discovery_status_)
                    self->kr_discovery_status_->setText(self->tr("Unexpected KR batch execution mode"));
                return;
            }
            const int selected = data.value("selected").toArray().size();
            const int completed = data.value("results").toArray().size();
            const int failed = data.value("errors").toObject().size() + data.value("input_errors").toObject().size();
            if (self->kr_discovery_status_)
                self->kr_discovery_status_->setText(
                    self->tr("Completed KR Top-N research · %1/%2 decisions · %3 error(s) · research_only")
                        .arg(completed)
                        .arg(selected)
                        .arg(failed));
            if (self->kr_discovery_result_)
                self->kr_discovery_result_->setPlainText(
                    QString::fromUtf8(QJsonDocument(data).toJson(QJsonDocument::Indented)));
        });
}

QFrame* EquityAnalysisTab::build_kr_research_panel_() {
    auto* panel = make_panel_(QT_TR_NOOP("KR AI RESEARCH"), ui::colors::CYAN());
    auto* vl = static_cast<QVBoxLayout*>(panel->layout());

    auto* description = new QLabel(
        tr("Deep research for Korean listings using KIS market/foreign-institution flow and optional "
           "DART, Naver News and ECOS enrichment. Analysis is on-demand and research-only."));
    description->setWordWrap(true);
    vl->addWidget(description);

    auto* controls = new QWidget(nullptr);
    auto* hl = new QHBoxLayout(controls);
    hl->setContentsMargins(0, 0, 0, 0);
    hl->setSpacing(10);

    kr_research_btn_ = new QPushButton(tr("RUN KR AI DEEP RESEARCH"));
    kr_research_btn_->setCursor(Qt::PointingHandCursor);
    connect(kr_research_btn_, &QPushButton::clicked, this, &EquityAnalysisTab::on_kr_research_clicked);
    hl->addWidget(kr_research_btn_);

    kr_status_ = new QLabel(tr("On-demand · research_only · no live order submission"));
    hl->addWidget(kr_status_, 1);
    vl->addWidget(controls);

    kr_result_ = new QPlainTextEdit;
    kr_result_->setReadOnly(true);
    kr_result_->setPlaceholderText(tr("Run KR AI Deep Research to view the point-in-time research result here."));
    kr_result_->setMinimumHeight(240);
    vl->addWidget(kr_result_);
    return panel;
}

void EquityAnalysisTab::on_kr_research_clicked() {
    if (!is_korean_symbol_() || !kr_research_btn_ || !kr_result_)
        return;

    const QString market = kr_market_();
    if (market.isEmpty()) {
        kr_status_->setText(tr("KR market unresolved · wait for symbol info or use .KS/.KQ"));
        return;
    }
    const QString company_name = cached_info_.company_name.trimmed().isEmpty() ? kr_ticker_() : cached_info_.company_name;
    const QDateTime korea_now = QDateTime::currentDateTimeUtc().toOffsetFromUtc(9 * 60 * 60);
    const QDate korea_today = korea_now.date();
    QJsonObject payload{
        {"instrument", QJsonObject{{"ticker", kr_ticker_()},
                                   {"name", company_name},
                                   {"market", market},
                                   {"currency", "KRW"}}},
        {"analysis_date", korea_today.toString(Qt::ISODate)},
        {"analysis_cutoff_at", korea_now.toString(Qt::ISODateWithMs)},
        {"analysis_cutoff_mode", "live_request"},
        {"score", 0.0},
        {"strategy_id", "personal-kr-ui"},
    };
    const QJsonObject llm = fincept::services::equity::personal_kr_active_llm_config();
    if (!llm.isEmpty())
        payload["llm"] = llm;

    kr_research_btn_->setEnabled(false);
    kr_status_->setText(tr("Running KR AI research…"));
    kr_result_->setPlainText(tr("Collecting Korean market data and running the research chain…"));

    const QByteArray input = QJsonDocument(payload).toJson(QJsonDocument::Compact);
    QPointer<EquityAnalysisTab> self(this);
    const QString launch_symbol = current_symbol_;
    python::PythonRunner::RunOptions run_opts;
    run_opts.timeout_ms = 20 * 60 * 1000;
    run_opts.stdin_data = input;
    python::PythonRunner::instance().run_with_options(
        "personal_kr_terminal.py", {"analyze"}, run_opts,
        [self, launch_symbol](python::PythonResult result) {
            if (!self)
                return;
            if (self->current_symbol_ != launch_symbol)
                return;
            if (self->kr_research_btn_)
                self->kr_research_btn_->setEnabled(true);
            if (!result.success) {
                if (self->kr_status_)
                    self->kr_status_->setText(self->tr("KR AI research unavailable"));
                if (self->kr_result_)
                    self->kr_result_->setPlainText(result.error);
                return;
            }

            const QJsonDocument doc = QJsonDocument::fromJson(python::extract_json(result.output).toUtf8());
            if (!doc.isObject() || !doc.object().value("success").toBool(false)) {
                const QString error = doc.isObject() ? doc.object().value("error").toString() : result.output;
                if (self->kr_status_)
                    self->kr_status_->setText(self->tr("KR AI research unavailable"));
                if (self->kr_result_)
                    self->kr_result_->setPlainText(error);
                return;
            }
            const QJsonObject data = doc.object().value("data").toObject();
            const QString signal = data.value("signal").toString();
            if (signal != QLatin1String("Buy") && signal != QLatin1String("Hold") && signal != QLatin1String("Sell")) {
                if (self->kr_status_)
                    self->kr_status_->setText(self->tr("KR AI research unavailable"));
                if (self->kr_result_)
                    self->kr_result_->setPlainText(self->tr("Invalid Portfolio Manager signal contract"));
                return;
            }
            if (self->kr_status_)
                self->kr_status_->setText(self->tr("Completed · signal: %1 · research_only").arg(signal));
            if (self->kr_result_)
                self->kr_result_->setPlainText(QString::fromUtf8(QJsonDocument(data).toJson(QJsonDocument::Indented)));
        });
}

bool EquityAnalysisTab::is_korean_symbol_() const {
    const QString symbol = current_symbol_.trimmed().toUpper();
    auto is_six_digits = [](const QString& value) {
        if (value.size() != 6)
            return false;
        for (const QChar ch : value) {
            if (!ch.isDigit())
                return false;
        }
        return value != QLatin1String("000000");
    };
    if (is_six_digits(symbol))
        return true;
    if ((symbol.endsWith(QLatin1String(".KS")) || symbol.endsWith(QLatin1String(".KQ"))) && symbol.size() == 9)
        return is_six_digits(symbol.left(6));
    return false;
}

QString EquityAnalysisTab::kr_ticker_() const {
    const QString symbol = current_symbol_.trimmed().toUpper();
    return symbol.size() >= 6 ? symbol.left(6) : symbol;
}

QString EquityAnalysisTab::kr_market_() const {
    const QString symbol = current_symbol_.trimmed().toUpper();
    const QString exchange = cached_info_.exchange.trimmed().toUpper();
    if (symbol.endsWith(QLatin1String(".KQ")) || exchange.contains(QLatin1String("KOSDAQ")) ||
        exchange == QLatin1String("KOE"))
        return QStringLiteral("KOSDAQ");
    if (symbol.endsWith(QLatin1String(".KS")) || exchange.contains(QLatin1String("KOSPI")) ||
        exchange == QLatin1String("KSC"))
        return QStringLiteral("KOSPI");
    return {};
}

QFrame* EquityAnalysisTab::build_hero_() {
    auto* panel = make_panel_(QT_TR_NOOP("ANALYST PRICE TARGET"), ui::colors::AMBER());
    auto* vl = static_cast<QVBoxLayout*>(panel->layout());

    // Headline row: now-price + upside, with the recommendation badge on the right.
    auto* head = new QWidget(nullptr);
    head->setStyleSheet("background:transparent;");
    auto* hl = new QHBoxLayout(head);
    hl->setContentsMargins(0, 0, 0, 0);
    hl->setSpacing(12);

    auto* left = new QVBoxLayout;
    left->setSpacing(2);
    hero_price_ = new QLabel(QStringLiteral("—"));
    hero_price_->setStyleSheet(QString("color:%1; font-size:26px; font-weight:800; font-family:monospace; "
                                       "background:transparent; border:0;")
                                   .arg(ui::colors::TEXT_PRIMARY()));
    hero_upside_ = new QLabel(QStringLiteral("—"));
    hero_upside_->setStyleSheet(QString("color:%1; font-size:15px; font-weight:700;"
                                        "background:transparent; border:0;")
                                    .arg(ui::colors::TEXT_TERTIARY()));
    left->addWidget(hero_price_);
    left->addWidget(hero_upside_);
    hl->addLayout(left);
    hl->addStretch();

    auto* right = new QVBoxLayout;
    right->setSpacing(2);
    hero_reco_ = new QLabel(QStringLiteral("—"));
    hero_reco_->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    hero_reco_->setStyleSheet(QString("color:%1; font-size:18px; font-weight:800; letter-spacing:1px; "
                                      "background:transparent; border:0;")
                                  .arg(ui::colors::TEXT_PRIMARY()));
    hero_count_ = new QLabel(QString());
    hero_count_->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    hero_count_->setStyleSheet(QString("color:%1; font-size:13px; font-weight:600; "
                                       "background:transparent; border:0;")
                                   .arg(ui::colors::TEXT_TERTIARY()));
    right->addWidget(hero_reco_);
    right->addWidget(hero_count_);
    hl->addLayout(right);

    vl->addWidget(head);

    gauge_ = new AnalysisPriceTargetGauge;
    vl->addWidget(gauge_);

    // No-coverage fallback (hidden unless there is no analyst data).
    hero_empty_ = new QLabel(tr("No analyst coverage available."));
    hero_empty_->setStyleSheet(QString("color:%1; font-size:13px; font-style:italic; "
                                       "background:transparent; border:0;")
                                   .arg(ui::colors::TEXT_TERTIARY()));
    hero_empty_->setVisible(false);
    vl->addWidget(hero_empty_);
    i18n_labels_.insert(hero_empty_, QT_TR_NOOP("No analyst coverage available."));

    return panel;
}

EquityAnalysisTab::VerdictCard EquityAnalysisTab::build_verdict_card_(QWidget* panel, const char* title_key,
                                                                      const QString& /*accent*/) {
    VerdictCard card;
    card.title_key = title_key;
    auto* vl = static_cast<QVBoxLayout*>(panel->layout());

    card.rating = new QLabel(QStringLiteral("—"));
    card.rating->setStyleSheet(QString("color:%1; font-size:26px; font-weight:800; letter-spacing:1px; "
                                       "background:transparent; border:0; padding:2px 0 6px 0;")
                                   .arg(ui::colors::TEXT_TERTIARY()));
    vl->addWidget(card.rating);

    // Metric lines grouped tightly together (their own box) so they read as a
    // block rather than drifting apart at the panel's wider spacing.
    auto* lines_box = new QWidget(nullptr);
    lines_box->setStyleSheet("background:transparent;");
    auto* lines_vl = new QVBoxLayout(lines_box);
    lines_vl->setContentsMargins(0, 0, 0, 0);
    lines_vl->setSpacing(6);
    for (auto& line : card.lines) {
        line = new QLabel(QString());
        line->setStyleSheet(QString("color:%1; font-size:15px; font-weight:600; font-family:monospace; "
                                    "background:transparent; border:0;")
                                .arg(ui::colors::TEXT_PRIMARY()));
        lines_vl->addWidget(line);
    }
    vl->addWidget(lines_box);

    card.rationale = new QLabel(QString());
    card.rationale->setWordWrap(true);
    card.rationale->setStyleSheet(QString("color:%1; font-size:13px; font-weight:500; line-height:140%; "
                                          "background:transparent; border:0; padding-top:4px;")
                                      .arg(ui::colors::TEXT_SECONDARY()));
    vl->addWidget(card.rationale);

    vl->addStretch();
    return card;
}

// ── Populate ────────────────────────────────────────────────────────────────────

void EquityAnalysisTab::on_info_loaded(services::equity::StockInfo info) {
    if (info.symbol != current_symbol_)
        return;
    cached_info_ = info;
    info_loaded_ = true;
    loading_overlay_->hide_loading();
    if (kr_research_btn_)
        kr_research_btn_->setEnabled(!kr_market_().isEmpty());

    populate_hero_(info);

    apply_verdict_(cards_[kValuation], assess_valuation_(info));
    apply_verdict_(cards_[kHealth], assess_health_(info));
    apply_verdict_(cards_[kCashFlow], assess_cashflow_(info));
    apply_verdict_(cards_[kProfitability], assess_profitability_(info));
    apply_verdict_(cards_[kGrowth], assess_growth_(info));
    apply_verdict_(cards_[kRisk], assess_risk_(info));
}

void EquityAnalysisTab::populate_hero_(const services::equity::StockInfo& info) {
    const bool has_targets = info.analyst_count > 0 && (info.target_mean > 0.0 || info.target_high > 0.0);

    if (!has_targets) {
        gauge_->clear_data();
        gauge_->setVisible(false);
        hero_price_->setText(info.current_price > 0.0 ? fmt_money(info.current_price) : QStringLiteral("—"));
        hero_upside_->setVisible(false);
        hero_reco_->setText(QStringLiteral("—"));
        hero_reco_->setStyleSheet(QString("color:%1; font-size:18px; font-weight:800; letter-spacing:1px; "
                                          "background:transparent; border:0;")
                                      .arg(ui::colors::TEXT_TERTIARY()));
        hero_count_->setText(QString());
        hero_empty_->setVisible(true);
        return;
    }

    hero_empty_->setVisible(false);
    hero_upside_->setVisible(true);
    gauge_->setVisible(true);
    gauge_->set_data(info.target_low, info.target_mean, info.target_high, info.current_price, cur_symbol_());

    hero_price_->setText(info.current_price > 0.0 ? fmt_money(info.current_price) + tr(" now") : QStringLiteral("—"));

    if (info.current_price > 0.0 && info.target_mean > 0.0) {
        const double up = (info.target_mean - info.current_price) / info.current_price * 100.0;
        const QString sign = up >= 0.0 ? QStringLiteral("+") : QStringLiteral("-");
        hero_upside_->setText(QString("%1%2%  ").arg(sign).arg(std::abs(up), 0, 'f', 1) + tr("to mean target"));
        hero_upside_->setStyleSheet(QString("color:%1; font-size:15px; font-weight:700;"
                                            "background:transparent; border:0;")
                                        .arg(up >= 0.0 ? ui::colors::POSITIVE() : ui::colors::NEGATIVE()));
    } else {
        hero_upside_->setText(tr("target range shown"));
        hero_upside_->setStyleSheet(QString("color:%1; font-size:15px; font-weight:700;"
                                            "background:transparent; border:0;")
                                        .arg(ui::colors::TEXT_TERTIARY()));
    }

    // Recommendation badge.
    const QString key = info.recommendation_key.toLower();
    QString label;
    Tone tone = Tone::Neutral;
    if (key == "strong_buy") {
        label = tr("STRONG BUY");
        tone = Tone::Good;
    } else if (key == "buy") {
        label = tr("BUY");
        tone = Tone::Good;
    } else if (key == "hold" || key == "neutral") {
        label = tr("HOLD");
        tone = Tone::Caution;
    } else if (key == "underperform" || key == "sell") {
        label = tr("SELL");
        tone = Tone::Bad;
    } else if (key == "strong_sell") {
        label = tr("STRONG SELL");
        tone = Tone::Bad;
    } else {
        label = key.isEmpty() ? QStringLiteral("—") : info.recommendation_key.toUpper();
        tone = Tone::Neutral;
    }
    hero_reco_->setText(label);
    hero_reco_->setStyleSheet(QString("color:%1; font-size:18px; font-weight:800; letter-spacing:1px; "
                                      "background:transparent; border:0;")
                                  .arg(color_for_(tone)));

    QString count = tr("%n analyst(s)", "", info.analyst_count);
    if (info.recommendation_mean > 0.0)
        count = QString("%1  ·  ").arg(QString::number(info.recommendation_mean, 'f', 1)) + count;
    hero_count_->setText(count);
}

void EquityAnalysisTab::apply_verdict_(const VerdictCard& card, const Verdict& v) {
    card.rating->setText(tr(v.rating_key));
    card.rating->setStyleSheet(QString("color:%1; font-size:26px; font-weight:800; letter-spacing:1px; "
                                       "background:transparent; border:0; padding:2px 0 6px 0;")
                                   .arg(color_for_(v.tone)));

    const QString lines[3] = {v.line1, v.line2, v.line3};
    for (int i = 0; i < 3; ++i) {
        card.lines[i]->setText(lines[i]);
        card.lines[i]->setVisible(!lines[i].isEmpty());
    }
    card.rationale->setText(v.rationale);
    card.rationale->setVisible(!v.rationale.isEmpty());
}

// ── Assessment ────────────────────────────────────────────────────────────────
// Pure functions of StockInfo. Thresholds are deliberately simple, transparent,
// and market-cap/sector-agnostic — screening signals, not investment advice.

EquityAnalysisTab::Verdict EquityAnalysisTab::assess_valuation_(const services::equity::StockInfo& s) const {
    Verdict v;
    const bool has_pe = s.pe_ratio > 0.0;
    const bool has_peg = s.peg_ratio > 0.0;
    if (!has_pe && !has_peg && s.forward_pe <= 0.0) {
        v.rating_key = QT_TR_NOOP("N/A");
        v.tone = Tone::NA;
        v.rationale = tr("No earnings-based valuation available (may be unprofitable).");
        return v;
    }

    // Prefer PEG (growth-adjusted); fall back to raw P/E.
    int score = 0; // -1 expensive, 0 fair, +1 cheap
    if (has_peg) {
        if (s.peg_ratio < 1.0)
            score = 1;
        else if (s.peg_ratio <= 2.0)
            score = 0;
        else
            score = -1;
    } else if (has_pe) {
        if (s.pe_ratio < 15.0)
            score = 1;
        else if (s.pe_ratio <= 30.0)
            score = 0;
        else
            score = -1;
    }

    if (score > 0) {
        v.rating_key = QT_TR_NOOP("UNDERVALUED");
        v.tone = Tone::Good;
    } else if (score == 0) {
        v.rating_key = QT_TR_NOOP("FAIRLY VALUED");
        v.tone = Tone::Caution;
    } else {
        v.rating_key = QT_TR_NOOP("EXPENSIVE");
        v.tone = Tone::Bad;
    }

    QString pe_line = QStringLiteral("P/E ") + (has_pe ? fmt(s.pe_ratio, 1) : QStringLiteral("—"));
    if (s.forward_pe > 0.0) {
        const QString arrow = (has_pe && s.forward_pe < s.pe_ratio) ? QStringLiteral(" ↓") : QString();
        pe_line += QString("  fwd %1%2").arg(fmt(s.forward_pe, 1), arrow);
    }
    v.line1 = pe_line;
    v.line2 = QStringLiteral("PEG ") + (has_peg ? fmt(s.peg_ratio, 2) : QStringLiteral("—"));
    if (s.price_to_book > 0.0)
        v.line2 += QString("  ·  P/B %1").arg(fmt(s.price_to_book, 1));
    if (s.ev_to_ebitda > 0.0)
        v.line3 = QString("EV/EBITDA %1").arg(fmt(s.ev_to_ebitda, 1));

    if (has_pe && s.forward_pe > 0.0 && s.forward_pe < s.pe_ratio)
        v.rationale = tr("Forward P/E below trailing — earnings expected to grow.");
    else if (score < 0)
        v.rationale = tr("Trades at a premium; priced for growth or quality.");
    else if (score > 0)
        v.rationale = tr("Low multiple relative to earnings/growth.");
    else
        v.rationale = tr("Valuation in line with broad-market norms.");
    return v;
}

EquityAnalysisTab::Verdict EquityAnalysisTab::assess_health_(const services::equity::StockInfo& s) const {
    Verdict v;
    if (s.total_cash <= 0.0 && s.total_debt <= 0.0) {
        v.rating_key = QT_TR_NOOP("N/A");
        v.tone = Tone::NA;
        v.rationale = tr("Balance-sheet cash/debt not reported.");
        return v;
    }

    const double net = s.total_cash - s.total_debt;
    v.line1 = tr("Cash %1").arg(fmt_large(s.total_cash));
    v.line2 = tr("Debt %1").arg(fmt_large(s.total_debt));
    v.line3 = tr("Net %1%2").arg(net >= 0.0 ? QStringLiteral("+") : QStringLiteral("")).arg(fmt_large(net));

    if (net > 0.0) {
        v.rating_key = QT_TR_NOOP("STRONG");
        v.tone = Tone::Good;
        v.rationale = tr("Net cash position — more cash than total debt.");
    } else {
        const double ratio = s.total_cash > 0.0 ? s.total_debt / s.total_cash : 999.0;
        if (ratio < 2.0) {
            v.rating_key = QT_TR_NOOP("STABLE");
            v.tone = Tone::Caution;
            v.rationale = tr("Manageable leverage relative to cash on hand.");
        } else {
            v.rating_key = QT_TR_NOOP("STRETCHED");
            v.tone = Tone::Bad;
            v.rationale = tr("Debt is high relative to available cash.");
        }
    }
    return v;
}

EquityAnalysisTab::Verdict EquityAnalysisTab::assess_cashflow_(const services::equity::StockInfo& s) const {
    Verdict v;
    if (s.free_cashflow == 0.0 && s.operating_cashflow == 0.0) {
        v.rating_key = QT_TR_NOOP("N/A");
        v.tone = Tone::NA;
        v.rationale = tr("Cash-flow figures not reported.");
        return v;
    }

    v.line1 = tr("FCF %1").arg(fmt_large(s.free_cashflow));
    v.line2 = tr("Op CF %1").arg(fmt_large(s.operating_cashflow));
    if (s.total_revenue > 0.0)
        v.line3 = tr("FCF margin %1").arg(fmt_pct(s.free_cashflow / s.total_revenue));

    if (s.free_cashflow > 0.0) {
        v.rating_key = QT_TR_NOOP("STRONG");
        v.tone = Tone::Good;
        v.rationale = tr("Generates positive free cash flow after capex.");
    } else if (s.operating_cashflow > 0.0) {
        v.rating_key = QT_TR_NOOP("REINVESTING");
        v.tone = Tone::Caution;
        v.rationale = tr("Operating cash is positive but FCF is negative (heavy investment).");
    } else {
        v.rating_key = QT_TR_NOOP("BURNING CASH");
        v.tone = Tone::Bad;
        v.rationale = tr("Operations are consuming cash.");
    }
    return v;
}

EquityAnalysisTab::Verdict EquityAnalysisTab::assess_profitability_(const services::equity::StockInfo& s) const {
    Verdict v;
    const bool any = s.roe != 0.0 || s.roa != 0.0 || s.profit_margins != 0.0;
    if (!any) {
        v.rating_key = QT_TR_NOOP("N/A");
        v.tone = Tone::NA;
        v.rationale = tr("Profitability ratios not reported.");
        return v;
    }

    v.line1 = QString("ROE %1   ROA %2").arg(fmt_pct(s.roe), fmt_pct(s.roa));
    v.line2 = tr("Net margin %1").arg(fmt_pct(s.profit_margins));
    if (s.operating_margins != 0.0)
        v.line3 = tr("Oper. margin %1").arg(fmt_pct(s.operating_margins));

    if (s.profit_margins < 0.0) {
        v.rating_key = QT_TR_NOOP("LOSS-MAKING");
        v.tone = Tone::Bad;
        v.rationale = tr("Currently unprofitable on a net basis.");
    } else if (s.roe > 0.20 && s.profit_margins > 0.15) {
        v.rating_key = QT_TR_NOOP("EXCELLENT");
        v.tone = Tone::Good;
        v.rationale = tr("High returns on equity and strong net margins.");
    } else if (s.roe > 0.10 || s.profit_margins > 0.08) {
        v.rating_key = QT_TR_NOOP("SOLID");
        v.tone = Tone::Good;
        v.rationale = tr("Healthy, consistent profitability.");
    } else {
        v.rating_key = QT_TR_NOOP("THIN");
        v.tone = Tone::Caution;
        v.rationale = tr("Profitable, but margins and returns are slim.");
    }
    return v;
}

EquityAnalysisTab::Verdict EquityAnalysisTab::assess_growth_(const services::equity::StockInfo& s) const {
    Verdict v;
    if (s.revenue_growth == 0.0 && s.earnings_growth == 0.0) {
        v.rating_key = QT_TR_NOOP("N/A");
        v.tone = Tone::NA;
        v.rationale = tr("Growth rates not reported.");
        return v;
    }

    v.line1 = tr("Revenue %1").arg(fmt_pct(s.revenue_growth));
    v.line2 = tr("Earnings %1").arg(fmt_pct(s.earnings_growth));

    const double g = s.revenue_growth != 0.0 ? s.revenue_growth : s.earnings_growth;
    if (g > 0.20) {
        v.rating_key = QT_TR_NOOP("HIGH GROWTH");
        v.tone = Tone::Good;
        v.rationale = tr("Top line expanding rapidly.");
    } else if (g >= 0.05) {
        v.rating_key = QT_TR_NOOP("MODERATE");
        v.tone = Tone::Caution;
        v.rationale = tr("Steady single-to-double-digit growth.");
    } else if (g >= -0.02) {
        v.rating_key = QT_TR_NOOP("FLAT");
        v.tone = Tone::Neutral;
        v.rationale = tr("Revenue is roughly unchanged year over year.");
    } else {
        v.rating_key = QT_TR_NOOP("DECLINING");
        v.tone = Tone::Bad;
        v.rationale = tr("Revenue is contracting year over year.");
    }
    return v;
}

EquityAnalysisTab::Verdict EquityAnalysisTab::assess_risk_(const services::equity::StockInfo& s) const {
    Verdict v;
    const bool has_beta = s.beta != 0.0;
    if (!has_beta && s.short_pct_of_float == 0.0 && s.week52_high <= 0.0) {
        v.rating_key = QT_TR_NOOP("N/A");
        v.tone = Tone::NA;
        v.rationale = tr("Risk metrics not reported.");
        return v;
    }

    if (has_beta)
        v.line1 = tr("Beta %1").arg(fmt(s.beta, 2));
    if (s.short_pct_of_float > 0.0)
        v.line2 = tr("Short %1 of float").arg(fmt_pct(s.short_pct_of_float));
    if (s.week52_high > s.week52_low && s.current_price > 0.0) {
        const double pos = (s.current_price - s.week52_low) / (s.week52_high - s.week52_low) * 100.0;
        v.line3 = tr("52w position %1%").arg(QString::number(std::clamp(pos, 0.0, 100.0), 'f', 0));
    }

    const bool high_short = s.short_pct_of_float > 0.10;
    if ((has_beta && s.beta > 1.5) || high_short) {
        v.rating_key = QT_TR_NOOP("ELEVATED");
        v.tone = Tone::Bad;
        v.rationale = high_short ? tr("Heavy short interest signals bearish positioning.")
                                 : tr("High beta — amplifies market moves.");
    } else if (has_beta && s.beta > 1.0) {
        v.rating_key = QT_TR_NOOP("MODERATE");
        v.tone = Tone::Caution;
        v.rationale = tr("Moves roughly in line with, or above, the market.");
    } else if (has_beta) {
        v.rating_key = QT_TR_NOOP("LOW");
        v.tone = Tone::Good;
        v.rationale = tr("Lower volatility than the broad market.");
    } else {
        // No beta — don't claim a volatility level we can't support.
        v.rating_key = QT_TR_NOOP("MODERATE");
        v.tone = Tone::Neutral;
        v.rationale = tr("Beta unavailable; based on limited risk signals.");
    }
    return v;
}

// ── Formatting ────────────────────────────────────────────────────────────────

QString EquityAnalysisTab::fmt(double v, int decimals) const {
    return v != 0.0 ? QString::number(v, 'f', decimals) : QStringLiteral("—");
}

QString EquityAnalysisTab::fmt_large(double v) {
    if (v == 0.0)
        return QStringLiteral("—");
    const bool neg = v < 0;
    const double a = std::abs(v);
    QString s;
    if (a >= 1e12)
        s = QString("%1T").arg(a / 1e12, 0, 'f', 2);
    else if (a >= 1e9)
        s = QString("%1B").arg(a / 1e9, 0, 'f', 2);
    else if (a >= 1e6)
        s = QString("%1M").arg(a / 1e6, 0, 'f', 1);
    else
        s = QString::number(a, 'f', 0);
    return neg ? "-" + s : s;
}

QString EquityAnalysisTab::fmt_pct(double v) const {
    return v != 0.0 ? QString("%1%").arg(v * 100.0, 0, 'f', 2) : QStringLiteral("—");
}

QString EquityAnalysisTab::cur_symbol_() const {
    const QString c = cached_info_.currency.toUpper();
    if (c == "INR")
        return QStringLiteral("₹");
    if (c == "EUR")
        return QStringLiteral("€");
    if (c == "GBP")
        return QStringLiteral("£");
    if (c == "JPY")
        return QStringLiteral("¥");
    if (c.isEmpty() || c == "USD")
        return QStringLiteral("$");
    return c + QStringLiteral(" ");
}

QString EquityAnalysisTab::fmt_money(double v) const {
    if (v == 0.0)
        return QStringLiteral("—");
    const int dec = std::abs(v) >= 1000.0 ? 0 : 2;
    return cur_symbol_() + QString::number(v, 'f', dec);
}

QString EquityAnalysisTab::color_for_(Tone t) const {
    switch (t) {
        case Tone::Good:
            return ui::colors::POSITIVE();
        case Tone::Caution:
            return ui::colors::AMBER();
        case Tone::Bad:
            return ui::colors::NEGATIVE();
        case Tone::Neutral:
            return ui::colors::TEXT_SECONDARY();
        case Tone::NA:
        default:
            return ui::colors::TEXT_TERTIARY();
    }
}

// ── Re-translation ──────────────────────────────────────────────────────────────
// Re-apply tr() to static labels, then replay cached info so all dynamic
// hero/verdict text picks up the new language too.

void EquityAnalysisTab::changeEvent(QEvent* event) {
    if (event->type() == QEvent::LanguageChange)
        retranslateUi();
    QWidget::changeEvent(event);
}

void EquityAnalysisTab::retranslateUi() {
    for (auto it = i18n_labels_.constBegin(); it != i18n_labels_.constEnd(); ++it)
        it.key()->setText(tr(it.value()));
    if (kr_research_btn_)
        kr_research_btn_->setText(tr("RUN KR AI DEEP RESEARCH"));
    if (kr_status_)
        kr_status_->setText(tr("On-demand · research_only · no live order submission"));
    if (kr_result_)
        kr_result_->setPlaceholderText(tr("Run KR AI Deep Research to view the point-in-time research result here."));
    if (info_loaded_)
        on_info_loaded(cached_info_);
}

} // namespace fincept::screens
