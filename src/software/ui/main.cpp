// AgriVision Edge · Day 6 —— Qt 界面（粘连分离三路对比 + phenotyping 表型交互）

//

// 基线：phenotyping 的 agri_morph_gui_5（四视图 + 目标列表 + 单粒详情卡）。本文件在它之上

//       增加任务书 segmentation C1/C2 要求的 Comparison 页：

//   · 左侧 原图 + YOLO bbox；右侧 改进分离实例轮廓（每个实例一种颜色 + Seed ID）

//   · 「固定分水岭 / 自适应」切换；点轮廓/列表双向选中

//   · 指标卡：Species / GT 真实粒数 / YOLO 数量 / 改进数量 / 绝对误差 / 分离耗时

//   · 耗时四段分开显示，缺测写 N/A（只有产出方给了值才显示数值）

//   · 图像列表 + 「只看失败案例」过滤；FAIL 行首 ⚠

//   · 数据 source = "mock" 时顶部常驻黄色 MOCK 横幅

//   数据契约：docs/segmentation_result_format.md（schema = agrivision.segmentation.comparison.v1）

//

// phenotyping 主功能（Image / Batch / Live 三模式、四视图、一键导出）原样保留在

// 「表型 (phenotyping)」页里，一行逻辑都没动 —— 任务书要求「不破坏 phenotyping 主功能」。

//

// 单位：未做毫米标定，bbox / contour 均为原图像素。

//

// --- 以下是 phenotyping 基线的说明 ---

//

// 基线：detection 的 agri_morph_gui_4（QProcess 拉起 Python 后端 + 轮询 live_status.json），

//       本文件在其上增加任务书 C1/C2 要求的 phenotyping 功能：

//   · 四视图 Original / FPGA Output / Detection · YOLO / Phenotyping

//   · SEED 目标列表（Seed ID / Confidence / Length / Width / Area，异常行首 ⚠）

//   · 单粒详情卡（Area / Length / Width / Perimeter / Aspect Ratio / Circularity /

//     Status，并直接引用 B 给的 status_reason）

//   · 双向选中：点列表 → 图中目标高亮；点 Detection / Phenotyping 图 → 反选列表行

//   · 一键导出 Annotated Image + CSV + JSON 到同一批次目录

//     YYYYMMDD_HHMMSS_batch_xxx

//

// 数据来源（两种结构都认，Qt 侧一律只读不重算）：

//   A. B 的原生输出 data/evidence/phenotyping/objects.json

//      {schema, units, detector, frames:[{frame_id, seed_count, letterbox, objects:[…]}],

//       objects:[展平…]}，单目标字段 bbox_xyxy_src / seed_id / status_reason …

//   B. C 侧契约 docs/phenotyping_result_format.md 的 objects.json

//      {schema_version, frame_id, seed_count, objects:[{bbox: [x1,y1,x2,y2], …}]}

//

// 单位：未做毫米标定，Length/Width/Perimeter 为 px，Area 为 px²；

//       Aspect Ratio、Circularity 无量纲。

#include <QtWidgets>

#include <QElapsedTimer>

#include <QProcessEnvironment>

#include <QSaveFile>

#include <QSet>

#include <algorithm>

#include <cmath>

#include <functional>

// ---------------------------------------------------------------- 数据模型

struct SeedObject {

    int id = 0;                  // 1 起；界面显示 Seed #001

    QString seedIdText;          // B 的 "Seed #001"（有就用，没有按 id 生成）

    double confidence = 0.0;

    QRectF bbox;                 // 原图像素坐标 [x1,y1,x2,y2]

    double areaPx = 0.0;         // px²

    double lengthPx = 0.0;       // px（最小外接旋转矩形长边）

    double widthPx = 0.0;        // px（短边）

    double perimeterPx = 0.0;    // px

    double aspectRatio = 0.0;    // 无量纲，Length/Width ≥ 1

    double circularity = 0.0;    // 无量纲，4πA/P²

    QString status;              // OK / MERGED / BROKEN_CONTOUR / INVALID / 空

    QString statusReason;        // B 的一句话原因，详情卡直接引用

    bool hasMetrics = false;     // live 检测只有 bbox，没有 6 项表型

};

struct FrameData {

    int frameId = -1;

    int seedCount = -1;          // JSON 声明的目标数（-1 = 未给）

    QVector<SeedObject> objects;

    QImage original;             // 原图（YOLO 输入）

    QImage fpga;                 // PL 管线输出（二值边缘图）

    QString imageDir;            // 这一帧的图取自哪个目录（可核对）

};

struct ViewState {

    QImage composite;

    double scale = 1.0;          // 图像像素 → 控件像素的总倍率（含适应缩放 × 用户倍率）

    QPointF offset;              // 图像左上角在控件坐标里的位置

    // 下面三个是「看图放大」用的：userZoom = 1 时行为和以前完全一样（等比缩放居中）

    double userZoom = 1.0;       // 用户额外放大的倍率，1 = 适应

    QPointF center = QPointF(0.5, 0.5);   // 视野中心，**归一化**图像坐标 0..1

    double fitScale = 1.0;       // 最近一次呈现时的适应倍率，滚轮反解新 scale 要用

};

// ------------------------------------------- segmentation 粘连分离三路对比数据模型

// 契约见 docs/segmentation_result_format.md。计数 / 绝对误差 / 耗时全部由产出方算好写进

// JSON，Qt 只读不重算 —— 这样界面上的数与 comparison.csv 天然一致，

// 也满足任务书 C2「Qt 不重复实现分水岭」。

struct CmpObject {

    int id = 0;

    QString seedId;                 // 产出方给的标签，缺了按 id 生成

    QRectF bbox;                    // 原图像素 [x1,y1,x2,y2]

    QPolygonF contour;              // 原图像素折线；YOLO 路为空

    double confidence = 0.0;

    bool hasConf = false;

    QString status;                 // OK / MERGED / BROKEN_CONTOUR / INVALID / UNRESOLVED

    int parentYoloId = -1;          // 由哪个 YOLO 框拆出来；未拆 = -1

    double areaPx = -1.0;           // 轮廓面积；-1 = 产出方没给

    QPointF labelPos;               // 标签锚点（轮廓质心，没有轮廓就用框中心）

    QString tag() const

    {

        return seedId.isEmpty() ? QString("Seed #%1").arg(id, 3, 10, QChar('0'))

                                : seedId;

    }

};

struct CmpMode {

    bool present = false;

    int count = -1;                 // -1 = 产出方没给 → 界面 N/A

    int absError = -1;              // -1 = 无 GT 或缺测 → 界面 N/A

    QVector<CmpObject> objects;

};

struct CmpImage {

    QString imageId, file, species, contactType, split, note, failNote, gtSource;

    int gtCount = -1;               // -1 = 这张图没有人工标注 → GT 一律不显示

    bool isFail = false;

    CmpMode modes[3];               // 0 = yolo, 1 = fixed, 2 = adaptive

    double tYolo = -1, tFixed = -1, tAdaptive = -1, tTotal = -1;

    // 可选扩展 timings_ms.total_yolo/fixed/adaptive：切到哪一路就显示哪一路的整链路

    // 耗时；缺省回落 tTotal（老契约只有单值）

    double tTotalMode[3] = {-1.0, -1.0, -1.0};

    QImage original;

};

// 实例轮廓配色：相邻实例颜色必须拉开，否则密集场景里分不出哪条线属于哪粒

static QColor instanceColor(int k)

{

    static const QColor pal[12] = {

        QColor(46, 125, 50), QColor(198, 104, 0), QColor(2, 119, 189),

        QColor(173, 20, 87), QColor(0, 131, 143), QColor(121, 85, 72),

        QColor(94, 53, 177), QColor(158, 120, 0), QColor(84, 110, 122),

        QColor(216, 67, 21), QColor(27, 94, 32), QColor(13, 71, 161)};

    return pal[((k % 12) + 12) % 12];

}

// 缺测一律显示 N/A —— 任务书 C2：只有实际有值的项目才显示数值

static QString intText(int v)

{

    return v < 0 ? QString("N/A") : QString::number(v);

}

static QColor statusColor(const QString &status)

{

    if (status == "OK") return QColor(30, 125, 52);

    if (status == "MERGED") return QColor(199, 119, 0);

    if (status == "BROKEN_CONTOUR") return QColor(179, 65, 31);

    if (status == "INVALID") return QColor(139, 26, 26);

    return QColor(96, 96, 96);       // 未知/未声明状态：中性灰

}

static QString displayTag(const SeedObject &o)

{

    if (!o.seedIdText.isEmpty()) return o.seedIdText;

    return QString("Seed #%1").arg(o.id, 3, 10, QChar('0'));

}

// 选中高亮色：必须和四种 status 色都不同，否则密集场景里分不出选中项

static const QColor kSelectColor(21, 101, 192);

class ClickableLabel : public QLabel

{

public:

    using QLabel::QLabel;

    std::function<void(QPoint)> onClicked;

    // 看图用的三个回调。不做成 Q_OBJECT/信号，是为了不引入 moc ——

    // 这个工程是 qmake 单文件编译，加了 moc 就得改 .pro 并处理 main.moc

    std::function<void(QPoint, int)> onWheel;     // (控件坐标, 角度增量 1/8 度)

    std::function<void(QPoint)> onDrag;           // 拖动中的控件坐标位移

    std::function<void()> onDoubleClicked;

    // 尺寸变了就得重画：适应倍率是「控件尺寸 ÷ 图像尺寸」算出来的，

    // 沿用旧倍率会把图裁掉一块。以前靠 400ms 刷新兜，拖分割条时会看到闪

    std::function<void()> onResized;

protected:

    void resizeEvent(QResizeEvent *event) override

    {

        QLabel::resizeEvent(event);

        if (onResized) onResized();

    }

    // 点击改到「松开」时才判定：按住拖动是平移，不该顺带选中一个目标。

    // 位移超过 5px 就当成拖动，不触发 onClicked。

    void mousePressEvent(QMouseEvent *event) override

    {

        m_pressed = true;

        m_moved = false;

        m_pressPos = event->position().toPoint();

        m_dragAnchor = m_pressPos;

        if (onWheel && event->button() == Qt::LeftButton)

            setCursor(Qt::ClosedHandCursor);

        QLabel::mousePressEvent(event);

    }

    void mouseMoveEvent(QMouseEvent *event) override

    {

        if (!m_pressed) return;

        const QPoint p = event->position().toPoint();

        if ((p - m_pressPos).manhattanLength() > 5) m_moved = true;

        if (m_moved && onDrag) {

            onDrag(p - m_dragAnchor);

            m_dragAnchor = p;

        }

        QLabel::mouseMoveEvent(event);

    }

    void mouseReleaseEvent(QMouseEvent *event) override

    {

        if (m_pressed && !m_moved && onClicked)

            onClicked(event->position().toPoint());

        m_pressed = false;

        updateCursor();

        QLabel::mouseReleaseEvent(event);

    }

    void mouseDoubleClickEvent(QMouseEvent *event) override

    {

        // 双击前必然先来一次 release（已经选过一次目标），这里只负责开大窗

        m_pressed = false;

        if (onDoubleClicked) onDoubleClicked();

        QLabel::mouseDoubleClickEvent(event);

    }

    void wheelEvent(QWheelEvent *event) override

    {

        if (onWheel) {

            onWheel(event->position().toPoint(), event->angleDelta().y());

            event->accept();

            return;

        }

        QLabel::wheelEvent(event);

    }

public:

    // 放大后给个手型提示「这里能拖」；适应态保持箭头，免得看起来像能点着玩

    void setZoomable(bool on)

    {

        m_zoomable = on;

        updateCursor();

        setMouseTracking(on);

    }

private:

    void updateCursor()

    {

        if (!m_zoomable) { unsetCursor(); return; }

        setCursor(m_pressed && m_moved ? Qt::ClosedHandCursor : Qt::OpenHandCursor);

    }

    bool m_pressed = false;

    bool m_moved = false;

    bool m_zoomable = false;

    QPoint m_pressPos, m_dragAnchor;

};

// 把 QScrollArea 里各个「会自己吃滚轮」的控件的滚轮事件转给滚动面板。

// 不这么做的话：鼠标停在 QComboBox / QSpinBox / QSlider 上滚轮 → 改的是那个控件的值，

// 面板纹丝不动；停在空白处滚 → 面板滚。同一个面板两种行为，用起来很别扭。

//

// 必须「重造」一个事件并把坐标换算到 viewport：直接转发原事件的话，

// 坐标还停在源控件的坐标系里，滚动条会按错误的坐标去滚，表现为乱跳。

class WheelForwarder : public QObject

{

public:

    WheelForwarder(QScrollArea *sa, QObject *parent) : QObject(parent), m_sa(sa) {}

protected:

    bool eventFilter(QObject *, QEvent *e) override

    {

        if (e->type() != QEvent::Wheel || !m_sa) return false;

        auto *we = static_cast<QWheelEvent *>(e);

        QWheelEvent fwd(m_sa->viewport()->mapFromGlobal(we->globalPosition()),

                        we->globalPosition(), we->pixelDelta(), we->angleDelta(),

                        we->buttons(), we->modifiers(), we->phase(), we->inverted());

        QApplication::sendEvent(m_sa->viewport(), &fwd);

        return true;    // 吃掉：绝不让它落到原控件上改值

    }

private:

    QScrollArea *m_sa;

};

// 侧栏里「文件名 + 路径」这类手动省略的两行标签：宽度由布局/分割条事后决定，

// 构造时算一次省略会拿到旧宽度 —— 文字被裁掉而不是出省略号。

// 宽度一变就重排一次；推迟到下一轮事件循环，避免拿到布局尚未更新的宽度。

class RelayoutOnResize : public QObject

{

public:

    explicit RelayoutOnResize(std::function<void()> fn, QObject *parent = nullptr)

        : QObject(parent), m_fn(std::move(fn)) {}

protected:

    bool eventFilter(QObject *, QEvent *e) override

    {

        if (e->type() == QEvent::Resize)

            QTimer::singleShot(0, this, [this]() { if (m_fn) m_fn(); });

        return false;

    }

private:

    std::function<void()> m_fn;

};

// 把面板里所有会吃滚轮的控件挂上过滤器。用 findChildren 扫一遍而不是逐个手写，

// 是为了将来往面板里加控件时不会漏。

static void guardWheel(QWidget *root, QScrollArea *sa)

{

    for (QWidget *w : root->findChildren<QWidget *>()) {

        if (qobject_cast<QScrollBar *>(w)) continue;   // 滚动条自己要吃滚轮

        if (qobject_cast<QComboBox *>(w) || qobject_cast<QAbstractSpinBox *>(w)

            || qobject_cast<QAbstractSlider *>(w))

            w->installEventFilter(new WheelForwarder(sa, w));   // 父对象 = w，随 w 释放

    }

}

// 把 ViewState 里的图按「适应 + 用户倍率 + 视野中心」画到 view 上。

// 全工程只此一处算 scale/offset：主界面六个视图和放大窗都用它，

// 所以点击坐标反算 (pos - offset) / scale 对所有视图都成立。

static void presentImage(ClickableLabel *v, ViewState *vs)

{

    const QImage &img = vs->composite;

    const QRect cr = v->contentsRect();

    if (img.isNull() || cr.width() < 8 || cr.height() < 8) {

        v->setPixmap(QPixmap());

        return;

    }

    // 适应倍率：整张图刚好塞进控件

    const double fit = qMin(double(cr.width()) / img.width(),

                            double(cr.height()) / img.height());

    const double sc = fit * vs->userZoom;

    const double w = img.width() * sc, h = img.height() * sc;

    // 先把「视野中心」对到控件中心，再夹住，保证放大后图不会滑出控件留出空白

    double x = cr.width() / 2.0 - vs->center.x() * w;

    double y = cr.height() / 2.0 - vs->center.y() * h;

    x = (w <= cr.width()) ? (cr.width() - w) / 2.0

                          : qBound(double(cr.width()) - w, x, 0.0);

    y = (h <= cr.height()) ? (cr.height() - h) / 2.0

                           : qBound(double(cr.height()) - h, y, 0.0);

    // 每次重画都新建 pixmap：QLabel 不会替我们擦掉上一次的画面

    QPixmap pm(cr.size());

    pm.fill(Qt::transparent);

    {

        QPainter p(&pm);

        p.setRenderHint(QPainter::SmoothPixmapTransform, true);

        p.drawImage(QRectF(x, y, w, h), img, QRectF(img.rect()));

    }

    vs->scale = sc;

    vs->fitScale = fit;

    // pixmap 尺寸 == contentsRect，QLabel 居中后它左上角正好落在 cr.topLeft()，

    // 所以 offset 可以直接这样算，点击反算公式不用改

    vs->offset = QPointF(cr.topLeft()) + QPointF(x, y);

    v->setPixmap(pm);

    v->setZoomable(vs->userZoom > 1.001);

}

// 就地缩放：保持光标底下的那个图像点不动。

// 这是「滚轮放大看得见细节」和「放大后瞄不准」的分水岭 —— 以控件中心缩放的话，

// 想看的地方会转着圈跑掉，得反复平移补偿。

static void zoomAt(ClickableLabel *v, ViewState *vs, QPoint pos, int angleDelta)

{

    if (vs->composite.isNull()) return;

    const QPointF ip = (QPointF(pos) - vs->offset) / vs->scale;   // 光标下的图像点

    vs->userZoom = qBound(1.0, vs->userZoom * std::pow(1.0015, angleDelta), 12.0);

    if (vs->userZoom <= 1.0001) {

        vs->userZoom = 1.0;

        vs->center = QPointF(0.5, 0.5);   // 回到适应态就复位居中

    } else {

        const QRect cr = v->contentsRect();

        const double sc = vs->fitScale * vs->userZoom;

        // 反解：让 ip 在新映射下仍落在 pos 上。

        // 未夹紧时 offset = cr.topLeft() + cr中心 - center * 图像尺寸 * sc

        vs->center = QPointF(

            (cr.x() + cr.width() / 2.0 + ip.x() * sc - pos.x())

                / (vs->composite.width() * sc),

            (cr.y() + cr.height() / 2.0 + ip.y() * sc - pos.y())

                / (vs->composite.height() * sc));

        // 夹在 0..1：边界处夹紧后实际偏移和公式不符，不夹的话会一点点漂走

        vs->center.setX(qBound(0.0, vs->center.x(), 1.0));

        vs->center.setY(qBound(0.0, vs->center.y(), 1.0));

    }

    presentImage(v, vs);

}

// 用户倍率上限。12 倍 × 适应倍率已经能看到原本看不见的细节，

// 再大只是马赛克；上限也防着手抖一直滚把图缩到缝里找不回来。

static const double kMaxUserZoom = 16.0;

// ---------------------------------------------------------------- 放大窗

// 双击视图卡片弹出的网格大窗。非模态（不挡住主窗口，演示时两边都能用），

// 每格独立滚轮缩放 + 拖动平移，可勾「同步缩放」让所有格一起动。

//

// 同样不继承 Q_OBJECT：全程用 lambda 连接，避免引入 moc。

class ImageViewerDialog : public QDialog

{

public:

    struct Item {

        QString title;

        QImage image;

    };

    ImageViewerDialog(QWidget *parent, const QVector<Item> &items, int cols,

                      int startIdx = 0)

        : QDialog(parent), m_cols(qMax(1, cols))

    {

        setWindowTitle("图像放大查看");

        setAttribute(Qt::WA_DeleteOnClose);

        setMinimumSize(760, 520);

        // 默认铺得跟主窗口差不多大而不是缩到最小：放大窗的意义就是「看得清」，

        // 开出来只有巴掌大，用户还得先自己拖大

        {

            const QRect pg = parent ? parent->geometry() : QRect(0, 0, 1280, 800);

            const int w = qBound(760, int(pg.width() * 0.92), 1600);

            const int h = qBound(520, int(pg.height() * 0.92), 1000);

            resize(w, h);

            if (parent) move(pg.center() - QPoint(w / 2, h / 2));

        }

        auto *vl = new QVBoxLayout(this);

        vl->setContentsMargins(12, 10, 12, 10);

        vl->setSpacing(8);

        auto *bar = new QHBoxLayout;

        auto *head = new QLabel("图像放大查看");

        head->setObjectName("caption");

        bar->addWidget(head);

        auto *tip = new QLabel("滚轮缩放 · 按住拖动平移 · Esc 关闭");

        tip->setObjectName("hint");

        bar->addWidget(tip);

        bar->addStretch();

        auto mkBtn = [&](const QString &text, const QString &tooltip) {

            auto *b = new QToolButton;

            b->setObjectName("viewTool");

            b->setText(text);

            b->setToolTip(tooltip);

            b->setCursor(Qt::PointingHandCursor);

            bar->addWidget(b);

            return b;

        };

        // 这四个都是「对所有格一起生效」——不做「当前格」的话就不用跟踪焦点，

        // 单格缩放直接用滚轮，语义反而更清楚

        auto *btnIn = mkBtn("＋ 放大", "所有窗格一起放大（Ctrl+=）");

        auto *btnOut = mkBtn("－ 缩小", "所有窗格一起缩小（Ctrl+-）");

        auto *btnOne = mkBtn("1:1", "按原始像素 1:1 显示（Ctrl+1）");

        auto *btnFit = mkBtn("适应窗口", "整张图刚好塞进窗格（Ctrl+0）");

        m_sync = new QCheckBox("同步缩放");

        m_sync->setToolTip(

            "默认每格各看各的，方便逐个细看；勾上后任一格缩放/平移，其余跟着动 ——\n"

            "左右对比时用这个：两边始终锁在同一块区域、同一倍率");

        bar->addWidget(m_sync);

        // 实时链路的放大窗：默认跟随新帧，不勾就定格在当前这一帧。

        // 这两个控件只在「实时结果」的大窗里显示，对比页不显示。

        m_follow = new QCheckBox("跟随实时");

        m_follow->setToolTip("勾上：每 200ms 换成本次运行的最新一帧，缩放/平移保持不动；\n"

                             "不勾：定格在打开大窗时的那一帧，方便讲解细节");

        m_follow->setChecked(true);

        m_follow->setVisible(false);

        bar->addWidget(m_follow);

        m_refresh = mkBtn("刷新", "立刻取一次最新画面（定格模式下用）");

        m_refresh->setVisible(false);

        m_timer = new QTimer(this);

        m_timer->setInterval(200);

        QObject::connect(m_timer, &QTimer::timeout, this, [this]() { refreshImages(); });

        vl->addLayout(bar);

        auto *grid = new QGridLayout;

        grid->setSpacing(10);

        for (int i = 0; i < items.size(); ++i) {

            auto *card = new QFrame;

            card->setObjectName("card");

            card->setMinimumSize(180, 140);

            auto *cl = new QVBoxLayout(card);

            cl->setContentsMargins(8, 8, 8, 8);

            cl->setSpacing(4);

            auto *capRow = new QHBoxLayout;

            auto *cap = new QLabel(items[i].title);

            cap->setObjectName("caption");

            cap->setWordWrap(true);

            capRow->addWidget(cap, 1);

            auto *hud = new QLabel("--%");

            hud->setObjectName("zoomHud");

            hud->setToolTip("显示倍率：相对原图像素。100% = 一个图像像素占一个屏幕像素");

            capRow->addWidget(hud, 0, Qt::AlignTop);

            cl->addLayout(capRow);

            auto *v = new ClickableLabel("（无图像）");

            v->setObjectName("view");

            v->setAlignment(Qt::AlignCenter);

            v->setMinimumSize(180, 120);

            v->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Ignored);

            cl->addWidget(v, 1);

            grid->addWidget(card, i / m_cols, i % m_cols);

            m_labels << v;

            m_huds << hud;

            m_states << ViewState();

            m_states.last().composite = items[i].image;

            const int idx = i;

            v->onResized = [this, idx]() {

                presentImage(m_labels[idx], &m_states[idx]);

                updateHud(idx);

            };

            v->onWheel = [this, idx](QPoint p, int d) {

                zoomAt(m_labels[idx], &m_states[idx], p, d);

                updateHud(idx);

                syncFrom(idx);

            };

            v->onDrag = [this, idx](QPoint delta) {

                ViewState *vs = &m_states[idx];

                if (vs->userZoom <= 1.001) return;

                const QRect cr = m_labels[idx]->contentsRect();

                const double w = vs->composite.width() * vs->scale;

                const double h = vs->composite.height() * vs->scale;

                // 往右拖 = 图往右走 = 视野中心在图上往左移

                if (w > cr.width())

                    vs->center.setX(qBound(0.0, vs->center.x() - delta.x() / w, 1.0));

                if (h > cr.height())

                    vs->center.setY(qBound(0.0, vs->center.y() - delta.y() / h, 1.0));

                presentImage(m_labels[idx], vs);

                syncFrom(idx);

            };

        }

        vl->addLayout(grid, 1);

        // 快捷键：键盘也能缩放，讲解时不用非得找到滚轮

        auto addKey = [&](const QKeySequence &k, std::function<void()> fn) {

            auto *sc = new QShortcut(k, this);

            QObject::connect(sc, &QShortcut::activated, this, fn);

        };

        addKey(QKeySequence("Ctrl+="), [&]() { zoomAll(1.25); });

        addKey(QKeySequence("Ctrl++"), [&]() { zoomAll(1.25); });

        addKey(QKeySequence("Ctrl+-"), [&]() { zoomAll(1 / 1.25); });

        addKey(QKeySequence("Ctrl+0"), [&]() { fitAll(); });

        addKey(QKeySequence("Ctrl+1"), [&]() { oneToOneAll(); });

        QObject::connect(btnIn, &QToolButton::clicked, this,

                         [&]() { zoomAll(1.25); });

        QObject::connect(btnOut, &QToolButton::clicked, this,

                         [&]() { zoomAll(1 / 1.25); });

        QObject::connect(btnFit, &QToolButton::clicked, this, [&]() { fitAll(); });

        QObject::connect(btnOne, &QToolButton::clicked, this, [&]() { oneToOneAll(); });

        QObject::connect(m_sync, &QCheckBox::toggled, this, [this](bool on) {

            if (on) syncFrom(0);   // 勾上的瞬间就对齐，别等下一次滚动

        });

        // 初始全部适应；要看某一格的细节由调用方（或用户）再放大

        Q_UNUSED(startIdx);

        fitAll();

    }

    // 供 --view-zoom 自检：把第 i 格设成 z 倍

    void setZoom(int i, double z)

    {

        if (i < 0 || i >= m_states.size()) return;

        m_states[i].userZoom = qBound(1.0, z, kMaxUserZoom);

        presentImage(m_labels[i], &m_states[i]);

        updateHud(i);

    }

    void fitAll()

    {

        for (int i = 0; i < m_states.size(); ++i) {

            m_states[i].userZoom = 1.0;

            m_states[i].center = QPointF(0.5, 0.5);

            presentImage(m_labels[i], &m_states[i]);

            updateHud(i);

        }

        if (m_sync && m_sync->isChecked()) syncFrom(0);

    }

    // 每次窗格尺寸变化都要重画：适应倍率依赖控件大小

    void relayout()

    {

        for (int i = 0; i < m_states.size(); ++i) {

            presentImage(m_labels[i], &m_states[i]);

            updateHud(i);

        }

    }

    // 实时结果用：传入「取最新四张图」的回调后，大窗可跟随刷新。

    // 不传就是原来的静态快照（对比页、离线载入都走这条）。

    void setLiveProvider(std::function<QVector<Item>()> provider)

    {

        m_provider = std::move(provider);

        if (!m_provider) return;

        m_follow->setVisible(true);

        m_refresh->setVisible(true);

        QObject::connect(m_follow, &QCheckBox::toggled, this, [this](bool on) {

            if (on) {

                refreshImages();

                m_timer->start();

            } else {

                m_timer->stop();

            }

        });

        QObject::connect(m_refresh, &QToolButton::clicked, this,

                         [this]() { refreshImages(); });

        if (m_follow->isChecked()) m_timer->start();

    }

    // 换图但保留每格的 userZoom/center：放大看细节时也能继续跟帧

    void refreshImages()

    {

        if (!m_provider) return;

        const QVector<Item> items = m_provider();

        const int n = qMin(items.size(), m_states.size());

        for (int i = 0; i < n; ++i) {

            if (items[i].image.isNull()) continue;   // 这一拍没读到图：保留上一帧

            if (m_states[i].composite.cacheKey() == items[i].image.cacheKey())

                continue;                            // 同一张图，不必重绘

            m_states[i].composite = items[i].image;

            presentImage(m_labels[i], &m_states[i]);

            updateHud(i);

        }

        if (m_sync && m_sync->isChecked()) syncFrom(0);

    }

protected:

    void resizeEvent(QResizeEvent *e) override

    {

        QDialog::resizeEvent(e);

        relayout();

    }

private:

    void oneToOneAll()

    {

        for (int i = 0; i < m_states.size(); ++i) {

            // 「1:1」= 总倍率 1.0，所以用户倍率要除掉当前的适应倍率

            const double f = m_states[i].fitScale > 0 ? m_states[i].fitScale : 1.0;

            m_states[i].userZoom = qBound(1.0, 1.0 / f, kMaxUserZoom);

            presentImage(m_labels[i], &m_states[i]);

            updateHud(i);

        }

    }

    // 按钮/快捷键用的整体缩放：以各格正中为锚点（滚轮才按光标位置锚定）

    void zoomAll(double factor)

    {

        for (int i = 0; i < m_states.size(); ++i) {

            ViewState *vs = &m_states[i];

            if (vs->composite.isNull()) continue;

            vs->userZoom = qBound(1.0, vs->userZoom * factor, kMaxUserZoom);

            if (vs->userZoom <= 1.0001) {

                vs->userZoom = 1.0;

                vs->center = QPointF(0.5, 0.5);

            }

            presentImage(m_labels[i], vs);

            updateHud(i);

        }

    }

    void syncFrom(int src)

    {

        if (!m_sync || !m_sync->isChecked()) return;

        if (src < 0 || src >= m_states.size()) return;

        for (int i = 0; i < m_states.size(); ++i) {

            if (i == src) continue;

            m_states[i].userZoom = m_states[src].userZoom;

            m_states[i].center = m_states[src].center;

            presentImage(m_labels[i], &m_states[i]);

            updateHud(i);

        }

    }

    void updateHud(int i)

    {

        const ViewState &vs = m_states[i];

        m_huds[i]->setText(vs.composite.isNull()

            ? QString("--%")

            : QString("%1%").arg(int(std::lround(vs.scale * 100.0))));

    }

    int m_cols = 2;

    QCheckBox *m_sync = nullptr;

    QCheckBox *m_follow = nullptr;

    QToolButton *m_refresh = nullptr;

    QTimer *m_timer = nullptr;

    std::function<QVector<Item>()> m_provider;

    QVector<ClickableLabel *> m_labels;

    QVector<QLabel *> m_huds;

    QVector<ViewState> m_states;

};

// 从可执行文件目录往上找仓库根下的文件（编译目录深浅变化都能定位）

static QString findRepoFile(const QString &relative)

{

    QDir dir(QCoreApplication::applicationDirPath());

    for (int i = 0; i < 6; ++i) {

        const QString candidate = dir.absoluteFilePath(relative);

        if (QFileInfo::exists(candidate)) return candidate;

        if (!dir.cdUp()) break;

    }

    return QString();

}

// 找某一帧的原图 / PL 结果图：按候选目录 × 候选文件名逐个试

static QString frameImagePath(const QStringList &dirs, int frameId, bool fpga)

{

    QStringList names;

    if (frameId >= 0) {

        const QString f6 = QString::number(frameId).rightJustified(6, '0');

        const QString f3 = QString::number(frameId).rightJustified(3, '0');

        if (fpga) {

            names << "pl_gray_" + f6 + ".png" << "pl_gray_" + f3 + ".png"

                  << "frame_" + f3 + "_fpga.png" << "fpga_" + f3 + ".png";

        } else {

            names << "raw_bgr_" + f6 + ".png" << "raw_bgr_" + f3 + ".png"

                  << "frame_" + f3 + "_original.png" << "original_" + f3 + ".png";

        }

    }

    names << (fpga ? "fpga.png" : "original.png");

    for (const QString &dir : dirs) {

        for (const QString &name : names) {

            const QString path = QDir(dir).filePath(name);

            if (QFileInfo::exists(path)) return path;

        }

    }

    return QString();

}

// 候选图像目录：显式指定 > JSON 所在目录 > 上级目录 > 仓库 hardware/*（*_pc 优先，

// B 的 README 指明正式数据是 hardware/image_run01_pc）

static QStringList buildImageDirs(const QString &jsonPath, const QString &explicitDir)

{

    QStringList dirs;

    auto add = [&](const QString &path) {

        if (path.isEmpty()) return;

        const QString clean = QDir(path).absolutePath();

        if (!dirs.contains(clean)) dirs << clean;

    };

    add(explicitDir);

    const QDir jsonDir = QFileInfo(jsonPath).absoluteDir();

    add(jsonDir.absolutePath());

    QDir up = jsonDir;

    QString root;

    for (int i = 0; i < 5; ++i) {

        if (!up.cdUp()) break;

        const bool isRepoRoot = QFileInfo(up.absoluteFilePath("LICENSE")).isFile() && QFileInfo(up.absoluteFilePath("src/software")).isDir();

        if (isRepoRoot) {

            add(up.absolutePath());   // 仓库根也当作候选（方便放 original.png/fpga.png）

            root = up.absolutePath();

            break;

        }

    }

    if (!root.isEmpty()) {

        const QString hardware = root + "/data/evidence/hardware";

        QStringList subs = QDir(hardware).entryList(QDir::Dirs | QDir::NoDotAndDotDot);

        subs.sort();

        for (const QString &s : subs)

            if (s == "image_run01_pc") add(hardware + "/" + s);

        for (const QString &s : subs)

            if (s.endsWith("_pc") && s != "image_run01_pc") add(hardware + "/" + s);

        for (const QString &s : subs)

            if (!s.endsWith("_pc") && s != "image_run01_pc") add(hardware + "/" + s);

    }

    return dirs;

}

int main(int argc, char *argv[])

{

    // 高分屏（125%/150% 缩放）下让字号按真实比例走：否则行高被压缩，控件会挤在一起

    QGuiApplication::setHighDpiScaleFactorRoundingPolicy(

        Qt::HighDpiScaleFactorRoundingPolicy::PassThrough);

    QApplication app(argc, argv);

    app.setStyle("Fusion");

    const QString windowsFonts = QDir(qEnvironmentVariable("WINDIR", "C:/Windows")).filePath("Fonts/msyh.ttc");
    const int appFont = QFontDatabase::addApplicationFont(windowsFonts);
    const QStringList families = QFontDatabase::applicationFontFamilies(appFont);
    app.setFont(QFont(families.isEmpty() ? "Microsoft YaHei UI" : families.first(), 10));

    // ------------------------------------------------------------ 启动参数

    QCommandLineParser cli;

    cli.setApplicationDescription("AgriVision种子智检系统（检测 / 粘连分离 / 单粒表型）");

    cli.addHelpOption();

    // 离线表型交互

    QCommandLineOption optLoad("load", "启动即载入检测结果文件（多帧 objects.json）", "json");

    QCommandLineOption optImages("images", "帧图像所在目录（默认自动探测）", "dir");

    QCommandLineOption optFrame("frame", "载入后切到第 N 帧（从 0 数，证据截图用）", "n", "0");

    QCommandLineOption optExport("export", "载入后把当前帧导出批次到该目录", "dir");

    QCommandLineOption optExportAll("export-all", "把载入的所有帧各导出一个批次", "dir");

    QCommandLineOption optShot("shot", "延时截图（有放大窗时截放大窗），界面自检用", "png");

    QCommandLineOption optShotDelay("shot-delay",

        "截图前等待毫秒（默认 1500；等算法跑完时用大一点）", "ms");

    QCommandLineOption optSelectRow("select-row", "合成一次“点列表行选目标”（选中信号自检）", "id");

    QCommandLineOption optClickSeed("click-seed", "合成一次“点图选目标”（点击坐标反算自检）", "id");

    QCommandLineOption optQuit("quit", "截图/导出完成后退出");

    // 真链路 / 后端

    QCommandLineOption optCapture("capture", "等界面就绪后截图到指定 PNG 再退出", "png");

    QCommandLineOption optCaptureTimeout("capture-timeout", "截图等待上限(ms)", "ms", "30000");

    QCommandLineOption optAutoStart("auto-start", "启动后自动开始（截图/无人值守用）");

    QCommandLineOption optAnalyzeLive("analyze-live",

        "实时模式：等首帧后立即触发一轮实时分析（自检用）");

    QCommandLineOption optTabSwitch("tab-switch",

        "自检：到点切页签，格式 ms:idx，可写多段用逗号分隔", "list");

    QCommandLineOption optWantPheno("want-pheno",

        "自检：是否勾选「生成单粒表型结果」（1/0）", "0/1", "1");

    QCommandLineOption optWantCmp("want-cmp",

        "自检：是否勾选「生成粘连分离结果」（1/0）", "0/1", "1");

    QCommandLineOption optMode("mode", "Live/Image/Batch", "mode", "Live");

    QCommandLineOption optSource("source", "board(板卡实时) / mock(本地回放)", "src", "mock");

    QCommandLineOption optInput("input", "单张图片路径 / 批量目录 / 回放目录", "path");

    QCommandLineOption optDetect("detect", "是否开启 AI 检测", "0/1", "1");

    QCommandLineOption optConf("conf", "置信度阈值", "float", "0.25");

    QCommandLineOption optDetector("detector", "mock / auto", "d", "mock");

    QCommandLineOption optFrames("frames", "Live 帧数(0=持续)", "n", "0");

    QCommandLineOption optBackend("backend", "后端适配器脚本", "py");

    QCommandLineOption optPython("python", "Python 解释器", "exe", "python");

    QCommandLineOption optHost("host", "板卡 IP", "ip", "192.168.2.99");

    QCommandLineOption optPort("port", "板卡端口", "p", "9000");

    QCommandLineOption optSize("size", "分辨率 WxH", "size", "640x480");

    QCommandLineOption optOutDir("out-dir", "本次运行证据目录", "dir");

    // 只影响界面外观（不同窗口尺寸/展开状态的证据截图用）

    QCommandLineOption optUiSize("ui-size", "窗口尺寸 WxH（默认按屏幕自适应）", "WxH");

    QCommandLineOption optUiExpand("ui-expand", "折叠区块与日志默认展开（证据截图用）");

    // 粘连分离三路对比

    QCommandLineOption optCmp("cmp", "启动即载入粘连分离对比 JSON（并切到对比页）", "json");

    QCommandLineOption optCmpIndex("cmp-index", "切到第 N 张图（从 0 数）", "n", "0");

    QCommandLineOption optCmpMode("cmp-mode", "分离模式 fixed / adaptive", "mode", "adaptive");

    QCommandLineOption optCmpNext("cmp-steps",

        "连续点 N 次「下一张」（连点/不卡死自检）", "n");

    QCommandLineOption optCmpClick("cmp-click",

        "合成一次「点实例轮廓」（坐标反算 + 选中自检）", "id");

    QCommandLineOption optCmpBad("cmp-bad", "启动即勾上「只看失败案例」");

    QCommandLineOption optImgSteps("img-steps",

        "Image 模式连续操作 N 次（切帧/选行/点图交替，不卡死自检）", "n");

    // 图片放大自检

    QCommandLineOption optViewZoom("view-zoom",

        "把第 i 个视图设成 z 倍「i:z」，如 2:3.0（就地放大自检）", "i:z");

    QCommandLineOption optInspect("inspect",

        "启动即打开放大窗，可选参数为起始窗格序号", "i", "0");

    // 侧栏页截图自检：0 采集 / 1 表型 / 2 对比 / 3 设置

    QCommandLineOption optSection("section",

        "启动即打开第 N 个活动栏页面（0采集/1设置）", "n");

    QCommandLineOption optSide("side", "启动即收起左侧栏（自检用）");

    cli.addOptions({optLoad, optImages, optFrame, optExport, optExportAll, optShot,

                    optShotDelay,

                    optSelectRow, optClickSeed, optQuit, optCapture, optCaptureTimeout,

                    optAutoStart, optAnalyzeLive, optTabSwitch,

                    optWantPheno, optWantCmp,

                    optMode, optSource, optInput,

                    optDetect, optConf,

                    optDetector, optFrames, optBackend, optPython, optHost, optPort,

                    optSize, optOutDir, optUiSize, optUiExpand,

                    optCmp, optCmpIndex, optCmpMode, optCmpNext, optCmpClick, optCmpBad,

                    optImgSteps, optViewZoom, optInspect, optSection, optSide});

    cli.process(app);

    const QString sizeText = cli.value(optSize);

    int runWidth = sizeText.section('x', 0, 0).toInt();

    int runHeight = sizeText.section('x', 1, 1).toInt();

    const QString appDirPath = QCoreApplication::applicationDirPath();

    // 默认后端 = network 网络后端：source=net 时自己监听 PC1 推来的 JPEG 帧，

    // 其余 source 原样转交给 detection 适配器（不改变已验证的 mock/board 链路）

    const QString adapterRel = "src/software/app/network_backend.py";

    QString backendDefault = findRepoFile(adapterRel);

    if (backendDefault.isEmpty())

        backendDefault = QDir(appDirPath).absoluteFilePath("../../../../" + adapterRel);

    const QString backendPath = cli.value(optBackend).isEmpty()

        ? backendDefault : cli.value(optBackend);

    const QString outDir = cli.value(optOutDir).isEmpty()

        ? QDir(appDirPath).absoluteFilePath("run_output") : cli.value(optOutDir);

    const QString capturePath = cli.value(optCapture);

    const QString shotPath = cli.value(optShot);

    const QString exportDir = cli.value(optExport);

    const QString exportAllDir = cli.value(optExportAll);

    const QString loadPath = cli.value(optLoad);

    const QString imagesArg = cli.value(optImages);

    const int captureTimeout = cli.value(optCaptureTimeout).toInt();

    const bool autoStart = cli.isSet(optAutoStart) || !capturePath.isEmpty();

    const QString uiSizeText = cli.value(optUiSize);

    const bool uiExpand = cli.isSet(optUiExpand);

    const QString cmpPath = cli.value(optCmp);

    // ------------------------------------------------------------ 窗口与样式

    QMainWindow win;

    win.setWindowTitle("AgriVision种子智检系统");

    {

        // 初始尺寸跟屏幕走：小屏/高缩放时不要把窗口撑出屏幕，否则控件会被压在一起

        const QRect avail = win.screen() ? win.screen()->availableGeometry()

                                         : QRect(0, 0, 1920, 1080);

        int wantW = qMin(1620, int(avail.width() * 0.92));

        int wantH = qMin(980, int(avail.height() * 0.92));

        if (!uiSizeText.isEmpty()) {

            wantW = uiSizeText.section('x', 0, 0).toInt();

            wantH = uiSizeText.section('x', 1, 1).toInt();

        }

        win.setMinimumSize(1120, 700);

        win.resize(qMax(wantW, 1120), qMax(wantH, 700));

    }

    win.setStyleSheet(R"(

        QMainWindow { background:#f3f6ee; }

        QWidget { color:#214630; }

        QFrame#header { background:#153e2b; border-radius:9px; }

        QFrame#header QLabel { color:white; font-size:19px; font-weight:700;

                      letter-spacing:1px; }

        QFrame#header QLabel#sub { font-size:12px; font-weight:400;

                      color:#b9d6c2; letter-spacing:0; }

        QFrame#card { background:white; border:1px solid #d8e3cf; border-radius:10px; }

        QFrame#ai { background:#eef7ea; border:1px solid #bcd8ae; border-radius:10px; }

        QFrame#phenotyping { background:#fdf6e7; border:1px solid #e3cfa5; border-radius:10px; }

        QFrame#divider { background:#e4ecdc; max-height:1px; min-height:1px; }

        QLabel#view { background:#edf3e6; border:1px dashed #bfd1b1;

                      border-radius:8px; color:#7f9871; }

        QLabel#big { font-size:26px; font-weight:700; color:#2c6b3f; }

        /* 模式分段按钮：三连一组，选中态填深绿 —— 比下拉框更能一眼看出「现在在哪个模式」 */

        QPushButton#segBtn { background:#eef4e9; border:1px solid #c7d9b8;

                      border-radius:0; padding:9px 4px; font-weight:700;

                      font-size:13px; color:#3c5a45; }

        /* 圆角只给两端：用动态属性而不是 :first-child —— 后者在 Qt 上

           依赖对象树顺序，布局一变就失效；属性选择器是确定的 */

        QPushButton#segBtn[segPos="left"] { border-top-left-radius:7px;

                      border-bottom-left-radius:7px; }

        QPushButton#segBtn[segPos="right"] { border-top-right-radius:7px;

                      border-bottom-right-radius:7px; }

        QPushButton#segBtn:checked { background:#2f6b3d; border-color:#2f6b3d;

                      color:white; }

        QPushButton#segBtn:hover:!checked { background:#dcebd0; }

        QPushButton#segBtn:disabled { color:#9aab90; background:#e9eee2; }

        QFrame#modeBox { background:#f8fbf5; border:1px solid #e2ebdb;

                      border-radius:8px; }

        /* 视图卡片右上角的小圆钮（适应 / 放大窗）—— 要小到不抢标题，又能点 */

        QToolButton#viewTool { background:#eef4e9; border:1px solid #c7d9b8;

                      border-radius:5px; color:#2c5a3c; font-size:12px;

                      font-weight:700; padding:2px 7px; }

        QToolButton#viewTool:hover:!disabled { background:#d8e9c6; }

        QToolButton#viewTool:disabled { color:#b3c0ab; background:#f2f5ee; }

        QLabel#zoomHud { background:rgba(21,62,43,190); color:white;

                      border-radius:4px; padding:2px 6px; font-size:11px;

                      font-weight:700; }

        QLabel#cmpBig { font-size:34px; font-weight:800; color:#1d5b34; }

        QLabel#chip { background:#eef4e9; border:1px solid #dbe6d1; border-radius:9px;

                      padding:3px 10px; color:#2c5a3c; font-size:12px; }

        QLabel#caption { font-size:14px; font-weight:700; }

        QLabel#consistency { font-size:12px; font-weight:700; }

        QLabel#detailKey { color:#5c7a63; }

        QLabel#detailValue { font-size:15px; font-weight:700; }

        QLabel#hint { color:#7d947f; font-size:12px; }

        QToolButton#sectionHead { background:#f6faf2; border:1px solid #e2ebdb;

                      border-radius:6px; color:#2c5a3c; font-weight:700;

                      padding:5px 8px; text-align:left; }

        QToolButton#sectionHead:hover { background:#ecf5e4; }

        QPushButton { background:#e7f0dc; padding:7px; border:1px solid #c7d9b8;

                      border-radius:6px; }

        QPushButton:hover { background:#d8e9c6; }

        QPushButton#start { background:#387342; color:white; }

        QPushButton#export { background:#8a5a19; color:white; }

        QPushButton:disabled { color:#9aab90; background:#e9eee2; }

        QLineEdit,QComboBox,QSpinBox { background:white; padding:5px;

                      border:1px solid #ccdaba; border-radius:5px; min-height:20px; }

        QPlainTextEdit { background:#f9fbf6; border:1px solid #d9e4cf; border-radius:6px; }

        QTableWidget { background:white; border:1px solid #dce6d5;

                      border-radius:6px; gridline-color:#eef3ea; }

        QHeaderView::section { background:#eef4e9; border:0; padding:5px;

                      font-weight:700; color:#2c5a3c; }

        QTableWidget::item:selected { background:#d9ecd2; color:#153e2d; }

        QScrollArea { background:transparent; border:0; }

        QScrollArea > QWidget > QWidget { background:transparent; }

        QScrollBar:vertical { background:#eef4e9; width:9px; border-radius:4px; }

        QScrollBar::handle:vertical { background:#bccfae; border-radius:4px;

                      min-height:30px; }

        QScrollBar::handle:vertical:hover { background:#9fba8c; }

        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }

        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {

                      background:transparent; }

        /* 分割条：不只是加宽，还要让人看出「这里能拖」——

           qlineargradient 在 handle 正中压一条深色槽线当抓手，不用图片资源。

           只加宽不画槽线的话，宽条看起来像普通留白，评委不会去拖它。 */

        QSplitter::handle { background:#dfe9d7; }

        QSplitter::handle:horizontal { width:16px;

                      background: qlineargradient(x1:0,y1:0,x2:1,y2:0,

                          stop:0 #dfe9d7, stop:0.36 #dfe9d7, stop:0.5 #9cba83,

                          stop:0.64 #dfe9d7, stop:1 #dfe9d7); }

        QSplitter::handle:vertical { height:14px;

                      background: qlineargradient(x1:0,y1:0,x2:0,y2:1,

                          stop:0 #dfe9d7, stop:0.36 #dfe9d7, stop:0.5 #9cba83,

                          stop:0.64 #dfe9d7, stop:1 #dfe9d7); }

        QSplitter::handle:hover { background:#a8c793; }

        QSplitter::handle:pressed { background:#7fa663; }

        /* ---- 粘连分离对比页 ---- */

        QTabWidget::pane { border:1px solid #d8e3cf; border-radius:8px;

                      background:#f7faf3; top:-1px; }

        QTabBar::tab { background:#e7f0dc; border:1px solid #c7d9b8;

                      border-bottom:0; border-top-left-radius:8px;

                      border-top-right-radius:8px; padding:11px 30px;

                      margin-right:4px; font-size:14px; font-weight:700;

                      color:#3c5a45; }

        QTabBar::tab:selected { background:white; color:#153e2d;

                      border-top:3px solid #2f6b3d; padding-top:8px; }

        QTabBar::tab:hover:!selected { background:#d8e9c6; }

        /* MOCK 横幅：模拟数据必须一眼可辨，不能和真实结果混在一起 */

        QFrame#mockBanner { background:#fff3cd; border:2px solid #d9a406;

                      border-radius:8px; }

        QFrame#mockBanner QLabel { color:#6b4b00; font-weight:700; }

        QLabel#cmpBigNA { font-size:22px; font-weight:800; color:#9aa79a; }

        QLabel#cmpBigCap { font-size:11px; color:#5c7a63; }

        QLabel#cmpNA { color:#9aa79a; }

        QLabel#cmpTitle { font-size:14px; font-weight:800; color:#2c5a3c; }

        QPushButton#cmpMode { background:#eef4e9; border:1px solid #c7d9b8;

                      border-radius:7px; padding:9px 6px; font-weight:700;

                      font-size:13px; color:#3c5a45; }

        QPushButton#cmpMode:checked { background:#2f6b3d; border-color:#2f6b3d;

                      color:white; }

        QPushButton#cmpMode:hover:!checked { background:#dcebd0; }

        QPushButton#cmpStep { background:#e7f0dc; padding:6px 10px; }

        QListWidget { background:white; border:1px solid #dce6d5; border-radius:6px;

                      font-size:12px; }

        /* font-size 必须两个状态都写死：只写在 ::item 上时，选中项会退回窗口默认

           字号（比未选中项大一号），宽一丁点就被省略号截成 "FAIL seed_071..." */

        QListWidget::item { padding:5px 4px; font-size:12px; }

        QListWidget::item:selected { background:#d9ecd2; color:#153e2d;

                      font-size:12px; }

        /* ---- 活动栏 / 任务侧栏 / 共用底部输出 ---- */

        QFrame#activity { background:#153e2b; border:none; border-radius:0; }

        QToolButton#actBtn { background:transparent; border:none;

                      border-left:3px solid transparent; color:#b9cec0;

                      font-size:10px; font-weight:600; padding:7px 0; }

        QToolButton#actBtn:hover { background:#1f4f37; color:white; }

        QToolButton#actBtn:checked { background:#2a5a40; color:white;

                      border-left:3px solid #9fd08a; }

        QLabel#sideTitle { font-size:16px; font-weight:800; color:#1e2d22; }

        QLabel#sideSub { font-size:11px; color:#7d8b7d; }

        QFrame#outputPanel { background:white; border-top:1px solid #d9e5d0;

                      border-radius:0; }

    )");

    auto *root = new QWidget;

    win.setCentralWidget(root);

    auto *vl = new QVBoxLayout(root);

    vl->setContentsMargins(18, 16, 18, 16);

    vl->setSpacing(8);

    auto *header = new QFrame;

    header->setObjectName("header");

    auto *head = new QHBoxLayout(header);

    head->addWidget(new QLabel("AgriVision种子智检系统"));

    head->addStretch();

    auto *subTitle = new QLabel("边缘端 FPGA 加速 · 目标检测 / 粘连分离 / 单粒表型");

    // 副标题降一档：让左边的品牌名站住主位，而不是两条同粗的白字互相抢

    subTitle->setObjectName("sub");

    head->addWidget(subTitle);

    vl->addWidget(header);

    // 状态行做成小胶囊并把高度钉住：live 时文字长短变化不会让整页上下抖

    // （这两条属于检测链路，放进「单粒表型测量」页里，不占对比页的版面）

    auto *status = new QLabel("就绪 · 载入检测结果查看表型测量，或点击「开始运行」执行检测链路");

    status->setObjectName("chip");

    status->setWordWrap(true);

    status->setMinimumHeight(24);

    auto *metrics = new QLabel("FRAME -- | Camera FPS -- | Live FPS -- | RTT -- | AI 推理 --");

    metrics->setObjectName("chip");

    metrics->setWordWrap(true);

    metrics->setMinimumHeight(24);

    // 折叠区块：点标题展开/收起。有选项的控件块常驻；纯说明信息默认收起，省版面。

    // 标题只放短词：QToolButton 不会换行，长标题会把整栏的最小宽度撑大、拖窄时被裁掉。

    auto makeCollapsible = [](QVBoxLayout *host, const QString &title,

                              bool expanded, const QString &tip = QString())

        -> QVBoxLayout * {

        auto *btn = new QToolButton;

        btn->setObjectName("sectionHead");

        btn->setCheckable(true);

        btn->setChecked(expanded);

        btn->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);

        // 横向 Ignored：按钮宽度跟着栏宽走，绝不反过来撑大栏宽

        btn->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);

        btn->setText(title);

        if (!tip.isEmpty()) btn->setToolTip(tip);

        auto *content = new QWidget;

        auto *lay = new QVBoxLayout(content);

        lay->setContentsMargins(6, 4, 2, 2);

        lay->setSpacing(5);

        content->setVisible(expanded);

        host->addWidget(btn);

        host->addWidget(content);

        QObject::connect(btn, &QToolButton::toggled, content,

                         [btn, content](bool on) {

                             content->setVisible(on);

                             btn->setArrowType(on ? Qt::DownArrow : Qt::RightArrow);

                         });

        btn->setArrowType(expanded ? Qt::DownArrow : Qt::RightArrow);

        return lay;

    };

    // 六个视图的呈现状态（四视图 + 对比页左右两路）在这里就声明：

    // 视图卡片的「适应」按钮和放大窗回调在下面建控件时就要用到它们

    ViewState viewStates[4];

    ViewState cmpLeftState, cmpRightState;

    // QPointer 而不是裸指针：大窗带 WA_DeleteOnClose，用户按 Esc 关掉后对象就没了，

    // 裸指针会在 --shot 里变成野指针（只在自检路径上踩得到，但很难查）

    QPointer<ImageViewerDialog> inspectDlg;    // 放大窗最多一个，重开时关掉旧的

    // 先声明后赋值（和下面 redraw 一样的手法）：视图卡片建好时就要连上放大窗，

    // 但放大窗要用到的对比页标题、上下两张图那时还没建出来

    std::function<void(bool, int)> openInspect;

    // 三栏用 QSplitter：分割条自带双向箭头光标，可拖着改各栏宽度

    auto *body = new QSplitter(Qt::Horizontal);

    body->setChildrenCollapsible(false);

    body->setHandleWidth(16);

    // ---------------- 左侧：活动栏 + 随任务切换的侧栏 ----------------

    // 仿编辑器布局：窄活动栏只放 4 个入口，具体选项放进会切换的侧栏。

    // 这样「采集 / 表型 / 对比 / 设置」各占一页，演示时不用在一个长列表里上下找。

    auto *sideWrap = new QWidget;

    auto *sideOuter = new QHBoxLayout(sideWrap);

    sideOuter->setContentsMargins(0, 0, 0, 0);

    sideOuter->setSpacing(0);

    auto *activity = new QFrame;

    activity->setObjectName("activity");

    // 只放图标（文字进 tooltip），宽度贴着图标走

    activity->setFixedWidth(42);

    auto *actLay = new QVBoxLayout(activity);

    actLay->setContentsMargins(0, 4, 0, 4);

    actLay->setSpacing(1);

    // 图标用 QPainter 现画，不引入图片资源（打包时不用额外带 icon 文件）

    auto actIcon = [](int kind) {

        QPixmap pm(48, 48);

        pm.fill(Qt::transparent);

        QPainter p(&pm);

        p.setRenderHint(QPainter::Antialiasing, true);

        p.setPen(QPen(QColor(214, 232, 217), 2.4, Qt::SolidLine,

                      Qt::RoundCap, Qt::RoundJoin));

        p.setBrush(Qt::NoBrush);

        const double s = 48.0;

        if (kind == 0) {                       // 采集：相机

            p.drawRoundedRect(QRectF(.16*s, .32*s, .68*s, .44*s), 4, 4);

            p.drawLine(QPointF(.36*s, .32*s), QPointF(.42*s, .24*s));

            p.drawLine(QPointF(.42*s, .24*s), QPointF(.58*s, .24*s));

            p.drawLine(QPointF(.58*s, .24*s), QPointF(.64*s, .32*s));

            p.drawEllipse(QPointF(.50*s, .54*s), .12*s, .12*s);

        } else if (kind == 1) {                // 表型：柱状图

            p.drawLine(QPointF(.18*s, .80*s), QPointF(.82*s, .80*s));

            p.drawLine(QPointF(.31*s, .76*s), QPointF(.31*s, .52*s));

            p.drawLine(QPointF(.50*s, .76*s), QPointF(.50*s, .32*s));

            p.drawLine(QPointF(.69*s, .76*s), QPointF(.69*s, .44*s));

        } else if (kind == 2) {                // 对比：并排两块

            p.drawRoundedRect(QRectF(.15*s, .20*s, .30*s, .60*s), 3, 3);

            p.drawRoundedRect(QRectF(.55*s, .20*s, .30*s, .60*s), 3, 3);

        } else {                               // 设置：三条滑杆

            for (int k = 0; k < 3; ++k) {

                const double y = (.32 + .18 * k) * s;

                p.drawLine(QPointF(.18*s, y), QPointF(.82*s, y));

                const double x = (k == 0 ? .38 : k == 1 ? .64 : .48) * s;

                p.setBrush(QColor(214, 232, 217));

                p.drawEllipse(QPointF(x, y), .075*s, .075*s);

                p.setBrush(Qt::NoBrush);

            }

        }

        return QIcon(pm);

    };

    // 活动栏只留「采集 / 设置」：表型的载入导出、对比的图像列表都已放回各自页面，

    // 侧栏不再承担与某个页签绑定的功能

    const QStringList actTexts = {"采集", "设置"};

    QVector<QToolButton *> actBtns;

    auto *actGroup = new QButtonGroup(&win);

    actGroup->setExclusive(true);

    for (int i = 0; i < actTexts.size(); ++i) {

        auto *b = new QToolButton;

        b->setObjectName("actBtn");

        b->setText(actTexts[i]);

        b->setIcon(actIcon(i == 0 ? 0 : 3));

        b->setIconSize(QSize(26, 26));

        b->setToolButtonStyle(Qt::ToolButtonIconOnly);

        b->setCheckable(true);

        b->setCursor(Qt::PointingHandCursor);

        b->setFixedHeight(52);

        b->setToolTip(actTexts[i] + "：点击切换左侧栏；再点一次收起");

        actGroup->addButton(b, i);

        actLay->addWidget(b);

        actBtns << b;

    }

    actLay->addStretch(1);

    sideOuter->addWidget(activity);

    auto *sideCard = new QFrame;

    sideCard->setObjectName("card");

    // 宽窄范围放宽：演示时既能收窄给图像让位，也能拉宽看清长路径

    sideCard->setMinimumWidth(240);

    sideCard->setMaximumWidth(760);

    auto *sideCardLay = new QVBoxLayout(sideCard);

    sideCardLay->setContentsMargins(0, 0, 0, 0);

    sideCardLay->setSpacing(0);

    auto *sideStack = new QStackedWidget;

    sideCardLay->addWidget(sideStack);

    sideOuter->addWidget(sideCard, 1);

    // sideWrap 不放 body：它要和页签平级，两个工作页共用同一条活动栏/侧栏，

    // 切到对比页时侧栏不能跟着消失（后面用外层横向分割条挂上去）

    // 每页自带标题和独立滚动区：某页内容长不会把别的页顶出视野

    QVector<QPair<QWidget *, QScrollArea *>> sidePages;

    auto makeSidePage = [&](const QString &title, const QString &sub) -> QVBoxLayout * {

        auto *scroll = new QScrollArea;

        scroll->setWidgetResizable(true);

        scroll->setFrameShape(QFrame::NoFrame);

        // AsNeeded 只是兜底：内容仍比栏宽时宁可出横向滚动条，也不要静默裁掉选项

        scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);

        auto *hostW = new QWidget;

        scroll->setWidget(hostW);

        auto *lay = new QVBoxLayout(hostW);

        lay->setContentsMargins(10, 8, 8, 8);

        lay->setSpacing(8);

        auto *t = new QLabel(title);

        t->setObjectName("sideTitle");

        auto *s = new QLabel(sub);

        s->setObjectName("sideSub");

        s->setWordWrap(true);

        lay->addWidget(t);

        lay->addWidget(s);

        sideStack->addWidget(scroll);

        sidePages.append({hostW, scroll});

        return lay;

    };

    QVBoxLayout *acqLay = makeSidePage("采集与检测", "选择输入并设置检测参数");

    QVBoxLayout *settingLay = makeSidePage("设置", "板卡、脚本与运行目录");

    QVBoxLayout *pl = acqLay;   // 下面各块按需切页

    auto field = [](QVBoxLayout *host, const QString &text, QWidget *w,

                    const QString &tip = QString()) {

        auto *lb = new QLabel(text);

        lb->setWordWrap(true);

        if (!tip.isEmpty()) {

            lb->setToolTip(tip);

            w->setToolTip(tip);

        }

        host->addWidget(lb);

        host->addWidget(w);

    };

    // 左栏宽、高度紧：标签和控件并排一行，比「标签在上、控件在下」省约 20px，

    // 否则单张/批量的「选择输入」会被挤到滚动区外面，切过去看不到入口。

    auto fieldRow = [](QVBoxLayout *host, const QString &text, QWidget *w,

                       const QString &tip = QString()) {

        auto *row = new QHBoxLayout;

        row->setSpacing(6);

        auto *lb = new QLabel(text);

        lb->setFixedWidth(72);

        if (!tip.isEmpty()) {

            lb->setToolTip(tip);

            w->setToolTip(tip);

        }

        row->addWidget(lb);

        row->addWidget(w, 1);

        host->addLayout(row);

    };

    // 检测结果入口放在最上面：演示时先点这两个

    auto *phenotypingBox = new QFrame;

    phenotypingBox->setObjectName("phenotyping");

    auto *d5 = new QVBoxLayout(phenotypingBox);

    d5->setContentsMargins(8, 8, 8, 8);

    d5->setSpacing(4);

    d5->addWidget(new QLabel("检测结果"));

    auto *openJson = new QPushButton("⇩ 载入结果");

    openJson->setToolTip("载入多帧检测结果文件（objects.json），可逐帧切换查看");

    auto *save = new QPushButton("导出结果");

    save->setObjectName("export");

    save->setEnabled(false);

    auto *frameBox = new QComboBox;

    frameBox->setEnabled(false);

    frameBox->setToolTip("结果文件里一帧一行；切换帧立刻刷新四视图、列表与详情卡");

    auto *loadRow = new QHBoxLayout;

    loadRow->setSpacing(4);

    loadRow->addWidget(openJson, 1);

    loadRow->addWidget(save, 1);

    d5->addLayout(loadRow);

    auto *frameRow = new QHBoxLayout;

    frameRow->setSpacing(6);

    frameRow->addWidget(new QLabel("帧"));

    frameRow->addWidget(frameBox, 1);

    d5->addLayout(frameRow);

    // 这个盒子下面挂到右检视栏顶部：载入/导出/帧切换都属于「看结果」，

    // 跟结果列表放在一起比塞在侧栏里顺手

    // ---------------- 运行模式：分段按钮 + 随选择展开的输入面板 ----------------

    // 原来「模式」是个下拉框，而「选择输入」埋在下面的折叠区里 —— 演示时得先展开

    // 折叠区才知道该填什么，两件事在版面上离得太远。现在点哪个模式，

    // 它要填的东西就出现在正下方，不用找。

    auto *modeTitle = new QLabel("运行模式");

    modeTitle->setWordWrap(true);

    pl->addWidget(modeTitle);

    const QStringList segNames = {"Live", "Image", "Batch"};

    const QStringList segTexts = {"实时采集", "单张图片", "批量目录"};

    // 模式值单独存一份：原来到处读 mode->currentText()，换成按钮后没有控件可读，

    // 全部改成读这个变量（后端请求、日志、文件对话框都靠它）

    QString modeValue = cli.value(optMode);

    if (!segNames.contains(modeValue)) modeValue = "Live";

    // 输入路径也单独存：原来它寄存在一个 QLabel 的文本里，

    // 「给人看的显示文本」和「真正的路径」混成一个东西，不好扩展

    QString inputPath = cli.value(optInput);

    auto *segRow = new QHBoxLayout;

    segRow->setSpacing(0);   // 三个按钮紧贴成一整条，看起来就是一组

    QVector<QPushButton *> segBtns;

    for (int i = 0; i < segTexts.size(); ++i) {

        auto *b = new QPushButton(segTexts[i]);

        b->setObjectName("segBtn");

        b->setCheckable(true);

        b->setCursor(Qt::PointingHandCursor);

        b->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);

        // 圆角只给两端，用动态属性挑（QSS 里写 #segBtn[segPos="left"]）

        b->setProperty("segPos", i == 0 ? "left"

                       : (i == segTexts.size() - 1 ? "right" : "mid"));

        segBtns << b;

        segRow->addWidget(b, 1);

    }

    pl->addLayout(segRow);

    // 三页输入面板直接挂在左栏上，用「显示 / 隐藏」切换，**不用 QStackedWidget**：

    // QStackedWidget 的高度按「最高的一页」算，切到内容少的一页时下方会留一大块空白，

    // 且页内那个自动折行的说明标签会把多出来的高度全吃掉 —— 文字被垂直居中画到

    // 可视区外面，屏幕上看起来就是「说明行没显示」。隐藏的页不占布局位置，

    // 于是面板高度永远等于当前页内容的高度。

    // 数据源三种模式都要用，放在分段按钮下方常驻 —— 以前它只藏在实时采集页里，

    // 切到单张/批量后想换板卡还是回放，界面上根本找不到入口。

    auto *source = new QComboBox;

    source->addItem("板卡实时", "board");

    source->addItem("本地回放", "mock");

    source->addItem("网络摄像头", "net");

    source->setCurrentIndex(qMax(0, source->findData(cli.value(optSource))));

    fieldRow(pl, "数据源", source,

             "板卡实时 = 连板卡跑真链路；本地回放 = 不开相机、不占板卡；"

             "网络摄像头 = 监听电脑1推来的 JPEG 帧（network 双机链路）");

    // 页 0：实时采集

    auto *livePage = new QFrame;

    livePage->setObjectName("modeBox");

    auto *liveLay = new QVBoxLayout(livePage);

    liveLay->setContentsMargins(4, 4, 4, 4);

    liveLay->setSpacing(4);

    auto *sizeBox = new QComboBox;

    sizeBox->addItems({"640x480", "320x240", "256x256"});

    sizeBox->setCurrentText(sizeText);

    sizeBox->setToolTip("送进实时采集链路的画面尺寸（等比缩放、空白补白）。"

                        "单张/批量按图片原始尺寸处理，不受这里影响。");

    auto *framesSpin = new QSpinBox;

    framesSpin->setRange(0, 100000);

    framesSpin->setValue(cli.value(optFrames).toInt());

    framesSpin->setSpecialValueText("持续运行");

    fieldRow(liveLay, "采集分辨率", sizeBox);

    fieldRow(liveLay, "帧数", framesSpin, "0 = 持续运行，直到点「停止」");

    // —— network：FPGA 处理模式（仅板卡源生效，状态回执来自 live_status.fpga/dma/preset）——

    auto *fpgaPreset = new QComboBox;

    fpgaPreset->addItem("Seed 预设（阈值40 + 膨胀）", "seed_preset");

    fpgaPreset->addItem("Sobel 阈值80 + 腐蚀", "sobel_thr80_erode");

    fpgaPreset->addItem("Sobel 阈值80 + 膨胀", "sobel_thr80_dilate");

    fpgaPreset->addItem("Prewitt 阈值80 + 腐蚀", "prewitt_thr80_erode");

    fpgaPreset->addItem("Prewitt 阈值0 + 膨胀", "prewitt_thr0_dilate");

    fieldRow(liveLay, "FPGA 模式", fpgaPreset,

             "只对「板卡实时」源生效；运行后下方显示板端实际回执的 FPGA/DMA/模式");

    auto *fpgaStatus = new QLabel("FPGA：未连接");

    fpgaStatus->setObjectName("hint");

    fpgaStatus->setWordWrap(true);

    liveLay->addWidget(fpgaStatus);

    // —— network：网络摄像头（电脑1 → 电脑2）——

    auto *netPort = new QSpinBox;

    netPort->setRange(1024, 65535);

    netPort->setValue(9100);

    fieldRow(liveLay, "网络端口", netPort,

             "网络摄像头源：电脑2 在本机端口监听电脑1推来的 JPEG 帧");

    auto *netStatus = new QLabel("网络：未启动");

    netStatus->setObjectName("hint");

    netStatus->setWordWrap(true);

    liveLay->addWidget(netStatus);

    pl->addWidget(livePage);

    // 分辨率下拉以前只显示、不生效（请求里用的是 --size）。现在改一下就把

    // width/height 一起改掉，下一次运行按新尺寸采集。

    QObject::connect(sizeBox, &QComboBox::currentTextChanged, sizeBox,

                     [&](const QString &text) {

        runWidth = text.section('x', 0, 0).toInt();

        runHeight = text.section('x', 1, 1).toInt();

    });

    // 页 1：单张图片

    auto *imgPage = new QFrame;

    imgPage->setObjectName("modeBox");

    auto *imgLay = new QVBoxLayout(imgPage);

    imgLay->setContentsMargins(4, 4, 4, 4);

    imgLay->setSpacing(4);

    auto *pickImage = new QPushButton("选择图片…");

    auto *pickImageInfo = new QLabel;

    pickImageInfo->setObjectName("hint");

    // 固定两行、手动省略：QLabel 自动折行在窄侧栏里高度算不准，

    // 第三行（图片尺寸）会被页面底边裁掉，看起来就是「字被挡住」。

    pickImageInfo->setWordWrap(false);

    pickImageInfo->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Minimum);

    imgLay->addWidget(pickImage);

    imgLay->addWidget(pickImageInfo);

    imgLay->addStretch(1);

    pl->addWidget(imgPage);

    // 页 2：批量目录

    auto *batchPage = new QFrame;

    batchPage->setObjectName("modeBox");

    auto *batchLay = new QVBoxLayout(batchPage);

    batchLay->setContentsMargins(4, 4, 4, 4);

    batchLay->setSpacing(4);

    auto *pickDir = new QPushButton("选择目录…");

    auto *pickDirInfo = new QLabel;

    pickDirInfo->setObjectName("hint");

    pickDirInfo->setWordWrap(false);

    pickDirInfo->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Minimum);

    batchLay->addWidget(pickDir);

    batchLay->addWidget(pickDirInfo);

    batchLay->addStretch(1);

    pl->addWidget(batchPage);

    // 把「当前选了什么」写到两个显示标签上；批量模式顺带数一下目录里有几张图

    auto refreshInputs = [&]() {

        // 左栏只有 ~240px 宽。这里固定输出两行，并**按像素手动省略**：

        // 自动折行在 QLabel 里高度算不准，第三个维度行会被裁掉；手动省略后

        // 两行的高度是确定的，拖侧栏宽度也不会再压字。

        auto resolve = [](const QString &p) {

            if (p.isEmpty() || QFileInfo::exists(p)) return p;

            const QString found = findRepoFile(p);

            return found.isEmpty() ? p : found;

        };

        auto elide = [](QLabel *lab, const QString &text) {

            // 永远按标签当前的内容宽度省略；宽度还没定下来时宁可短一点，

            // 也不能给出比控件更宽的字（那会被直接裁掉，连省略号都看不到）。

            const int w = qMax(20, lab->contentsRect().width() - 2);

            return lab->fontMetrics().elidedText(text, Qt::ElideMiddle, w);

        };

        auto describe = [&](const QString &p, bool wantDir) -> QString {

            if (p.isEmpty()) return QStringLiteral("（未选择）");

            const QString full = resolve(p);

            QLabel *lab = wantDir ? pickDirInfo : pickImageInfo;

            if (wantDir) {

                const int n = QDir(full).entryList({"*.png", "*.jpg", "*.jpeg", "*.bmp",

                                                    "*.tif", "*.tiff"},

                                                   QDir::Files).size();

                return QString("共 %1 张图片\n%2").arg(n).arg(elide(lab, full));

            }

            // 单张图顺手把原始尺寸显示出来：板卡链路按这个尺寸处理，

            // 和上面的「采集分辨率」无关。

            const QSize size = QImageReader(full).size();

            const QString dim = size.isValid()

                ? QString("%1×%2 px").arg(size.width()).arg(size.height())

                : QStringLiteral("尺寸未知");

            const QString head = QString("%1 · %2")

                                     .arg(QFileInfo(full).fileName(), dim);

            return elide(lab, head) + "\n" + elide(lab, full);

        };

        pickImageInfo->setText(describe(inputPath, false));

        pickImageInfo->setToolTip(inputPath);

        pickDirInfo->setText(describe(inputPath, true));

        pickDirInfo->setToolTip(inputPath);

    };

    // 拖分割条改变侧栏宽度后，按新宽度重新省略，避免文字被裁掉

    pickImageInfo->installEventFilter(

        new RelayoutOnResize([&]() { refreshInputs(); }, pickImageInfo));

    pickDirInfo->installEventFilter(

        new RelayoutOnResize([&]() { refreshInputs(); }, pickDirInfo));

    auto setMode = [&](const QString &m) {

        modeValue = m;

        for (int i = 0; i < segBtns.size(); ++i)

            segBtns[i]->setChecked(segNames[i] == m);

        const QVector<QWidget *> pages{livePage, imgPage, batchPage};

        for (int i = 0; i < pages.size(); ++i)

            pages[i]->setVisible(segNames[i] == m);

        refreshInputs();

    };

    for (int i = 0; i < segBtns.size(); ++i) {

        QObject::connect(segBtns[i], &QPushButton::clicked, segBtns[i],

                         [&, i]() { setMode(segNames[i]); });

    }

    QObject::connect(pickImage, &QPushButton::clicked, pickImage, [&]() {

        const QString p = QFileDialog::getOpenFileName(

            &win, "选择一张图片",

            QFileInfo(inputPath).absolutePath().isEmpty()

                ? outDir : QFileInfo(inputPath).absolutePath(),

            "图像 (*.png *.jpg *.jpeg *.bmp);;所有文件 (*)");

        if (p.isEmpty()) return;

        inputPath = p;

        refreshInputs();

    });

    QObject::connect(pickDir, &QPushButton::clicked, pickDir, [&]() {

        const QString p = QFileDialog::getExistingDirectory(

            &win, "选择图片目录",

            QFileInfo(inputPath).absolutePath().isEmpty()

                ? outDir : QFileInfo(inputPath).absolutePath());

        if (p.isEmpty()) return;

        inputPath = p;

        refreshInputs();

    });

    setMode(modeValue);

    auto *aiBox = new QFrame;

    aiBox->setObjectName("ai");

    auto *aiLayout = new QVBoxLayout(aiBox);

    aiLayout->setContentsMargins(10, 10, 10, 10);

    auto *detect = new QCheckBox("目标检测（YOLO）");

    detect->setChecked(cli.value(optDetect) != "0");

    aiLayout->addWidget(detect);

    auto *detector = new QComboBox;

    detector->addItems({"mock", "auto"});

    detector->setCurrentText(cli.value(optDetector));

    detector->setToolTip("实时采集链路用：mock = 占位检测框（不加载模型）；"

                         "auto = 接推理脚本加载真实模型。单张/批量统一分析固定用冻结检测器");

    auto *detectorLabel = new QLabel("检测器（实时）");

    detectorLabel->setToolTip(detector->toolTip());

    aiLayout->addWidget(detectorLabel);

    aiLayout->addWidget(detector);

    // 品种是检测与表型的共同输入：一次运行只用一种（检测参数按品种取）

    auto *species = new QComboBox;

    species->addItem("黄豆", "soybean");

    species->addItem("绿豆", "mungbean");

    species->addItem("红豆", "redbean");

    species->addItem("其他", "other");

    species->setToolTip("检测与表型按品种取参数；一次输入只用一种品种。"

                        "「其他」使用通用参数（未按品种校准）");

    auto *speciesLabel = new QLabel("品种");

    speciesLabel->setToolTip(species->toolTip());

    aiLayout->addWidget(speciesLabel);

    aiLayout->addWidget(species);

    // 人工点数只对单张图有意义：对比页用它算绝对误差，没填就显示 N/A

    auto *gtSpin = new QSpinBox;

    gtSpin->setRange(-1, 9999);

    gtSpin->setValue(-1);

    gtSpin->setSpecialValueText("未标注");

    gtSpin->setToolTip("人工真实粒数：只对单张图片生效；填了对比页才会显示绝对误差");

    auto *gtLabel = new QLabel("人工点数 GT");

    gtLabel->setToolTip(gtSpin->toolTip());

    aiLayout->addWidget(gtLabel);

    aiLayout->addWidget(gtSpin);

    // 结果分开勾选：只生成勾上的那一种，避免「只想要计数却被迫等表型」。

    // 两个都勾时同一次检测写两份 JSON，两个页签各自显示。

    auto *phenoChk = new QCheckBox("生成单粒表型结果");

    phenoChk->setChecked(cli.value(optWantPheno) != "0");

    phenoChk->setToolTip("勾上后产出单粒长度 / 宽度 / 面积等表型数据，"

                         "在「单粒表型测量」页查看");

    aiLayout->addWidget(phenoChk);

    auto *cmpChk = new QCheckBox("生成粘连分离结果");

    cmpChk->setChecked(cli.value(optWantCmp) != "0");

    cmpChk->setToolTip("勾上后产出三路分离计数与轮廓，"

                       "在「粘连分离对比」页查看");

    aiLayout->addWidget(cmpChk);

    auto *analysisHint = new QLabel(

        "单张/批量：点「开始分析」产出勾选的结果。"

        "实时：开始运行后自动分析最新帧，并刷新勾选的页签。");

    analysisHint->setObjectName("hint");

    analysisHint->setWordWrap(true);

    aiLayout->addWidget(analysisHint);

    auto *confLabel = new QLabel;

    auto *conf = new QSlider(Qt::Horizontal);

    conf->setRange(5, 95);

    conf->setValue(int(cli.value(optConf).toDouble() * 100));

    confLabel->setText(QString("置信度阈值 %1").arg(conf->value() / 100.0, 0, 'f', 2));

    aiLayout->addWidget(confLabel);

    aiLayout->addWidget(conf);

    pl->addWidget(aiBox);

    // GT 跟着模式走：批量与实时没有单张真值可填

    gtSpin->setEnabled(modeValue == "Image");

    for (QPushButton *b : segBtns)

        QObject::connect(b, &QPushButton::clicked, b,

                         [&]() { gtSpin->setEnabled(modeValue == "Image"); });

    // 高级设置：填一次就不动的项，默认折叠，点击标题才展开

    auto *devLay = makeCollapsible(

        settingLay, "板卡与脚本", uiExpand,

        "板卡 IP / Python 程序 / 后端脚本 / 本次运行目录 —— 点标题展开");

    auto *host = new QLineEdit(cli.value(optHost));

    field(devLay, "板卡 IP（端口 " + cli.value(optPort) + "）", host,

          "板卡的 IP；TCP 端口见括号，命令行 --port 可改");

    auto *python = new QLineEdit(cli.value(optPython));

    field(devLay, "Python 程序", python, "python 或 python.exe 的完整路径");

    auto *script = new QLineEdit(backendPath);

    field(devLay, "后端脚本", script,

          "默认 src/software/adapters/detection_adapter.py");

    auto *outDirEdit = new QLineEdit(outDir);

    field(devLay, "本次运行目录", outDirEdit);

    auto *folder = new QPushButton("打开运行结果目录");

    settingLay->addWidget(folder);

    // 每页末尾留伸缩：内容少的页不会被拉到顶部留白

    acqLay->addStretch(1);

    settingLay->addStretch(1);

    // —— 运行操作条：建好后挂到页签栏右上角，切页时位置不变 ——

    auto *start = new QPushButton("▶ 开始运行");

    start->setObjectName("start");

    auto *stop = new QPushButton("■ 停止");

    auto *restart = new QPushButton("↻ 重新开始");

    auto *netConnect = new QPushButton("🔗 连接网络");

    auto *netDisconnect = new QPushButton("✂ 断开网络");

    netConnect->setToolTip("网络摄像头源：启动后端并开始监听电脑1推来的帧");

    netDisconnect->setToolTip("停止后端进程，关闭监听端口");

    netConnect->setEnabled(false);

    netDisconnect->setEnabled(false);

    stop->setEnabled(false);

    restart->setEnabled(false);

    // 按钮文字跟着「当前点下去会发生什么」走：

    // 单张/批量 + 自动分析 = 一次出两份结果，叫「开始分析」比「开始运行」准确

    auto syncRunLabel = [&]() {

        const bool analyze = (modeValue == "Image" || modeValue == "Batch")

                             && detect->isChecked()

                             && (phenoChk->isChecked() || cmpChk->isChecked());

        start->setText(analyze ? "▶ 开始分析" : "▶ 开始运行");

        start->setToolTip(analyze

            ? "对当前输入跑一遍冻结检测器，只产出勾选的结果；"

              "另一页签若不是这次生成的，会保留上一次的结果"

            : "启动实时采集/板卡链路");

    };

    syncRunLabel();

    for (QPushButton *b : segBtns)

        QObject::connect(b, &QPushButton::clicked, b, syncRunLabel);

    QObject::connect(detect, &QCheckBox::toggled, syncRunLabel);

    QObject::connect(phenoChk, &QCheckBox::toggled, syncRunLabel);

    QObject::connect(cmpChk, &QCheckBox::toggled, syncRunLabel);

    // ---------------- 中间四视图 ----------------

    auto *mid = new QWidget;

    auto *gallery = new QGridLayout(mid);

    gallery->setSpacing(10);

    gallery->setContentsMargins(0, 0, 0, 0);

    body->addWidget(mid);

    // 标题要短：四视图被拖窄时，标题行右侧还有「适应 / ⤢」两个按钮，

    // 长标题会被卡片边缘切掉（曾经只剩「① 原图（检测」）

    const QStringList viewTitles = {"① 原图",

                                    "② FPGA 输出",

                                    "③ 目标检测",

                                    "④ 单粒表型"};

    QVector<ClickableLabel *> viewList;

    QVector<QToolButton *> viewFitBtns;

    for (int i = 0; i < viewTitles.size(); ++i) {

        auto *card = new QFrame;

        card->setObjectName("card");

        auto *cl = new QVBoxLayout(card);

        cl->setContentsMargins(10, 10, 10, 10);

        auto *caption = new QLabel(viewTitles[i]);

        caption->setObjectName("caption");

        // 窄窗时宁可折行，也不要让标题被右边缘吃掉；横向 Ignored 不反撑卡片

        caption->setWordWrap(true);

        caption->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);

        auto *view = new ClickableLabel("等待结果");

        view->setObjectName("view");

        view->setAlignment(Qt::AlignCenter);

        view->setMinimumSize(200, 120);

        view->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Ignored);

        // 标题行右侧两个小钮：适应（缩放归位）/ ⤢（放大窗）

        auto *capRow = new QHBoxLayout;

        capRow->setSpacing(4);

        capRow->addWidget(caption, 1);

        auto *btnFit = new QToolButton;

        btnFit->setObjectName("viewTool");

        btnFit->setText("适应");

        btnFit->setToolTip("缩放归位到「整张图刚好放得下」");

        btnFit->setEnabled(false);          // 没放大过就没什么可归位的

        auto *btnOpen = new QToolButton;

        btnOpen->setObjectName("viewTool");

        btnOpen->setText("⤢");

        btnOpen->setToolTip("放大查看四视图（双击图面同效）");

        capRow->addWidget(btnFit, 0, Qt::AlignTop);

        capRow->addWidget(btnOpen, 0, Qt::AlignTop);

        cl->addLayout(capRow);

        cl->addWidget(view, 1);

        viewList.append(view);

        viewFitBtns.append(btnFit);

        gallery->addWidget(card, i / 2, i % 2);

        const int idx = i;

        view->onWheel = [&, idx](QPoint p, int d) {

            zoomAt(viewList[idx], &viewStates[idx], p, d);

            viewFitBtns[idx]->setEnabled(viewStates[idx].userZoom > 1.001);

        };

        view->onDrag = [&, idx](QPoint delta) {

            ViewState *vs = &viewStates[idx];

            if (vs->userZoom <= 1.001) return;

            const QRect cr = viewList[idx]->contentsRect();

            const double w = vs->composite.width() * vs->scale;

            const double h = vs->composite.height() * vs->scale;

            if (w > cr.width())

                vs->center.setX(qBound(0.0, vs->center.x() - delta.x() / w, 1.0));

            if (h > cr.height())

                vs->center.setY(qBound(0.0, vs->center.y() - delta.y() / h, 1.0));

            presentImage(viewList[idx], vs);

        };

        view->onDoubleClicked = [&]() { openInspect(false, 0); };

        view->onResized = [&, idx]() {

            presentImage(viewList[idx], &viewStates[idx]);

            viewFitBtns[idx]->setEnabled(viewStates[idx].userZoom > 1.001);

        };

        QObject::connect(btnFit, &QToolButton::clicked, btnFit, [&, idx]() {

            viewStates[idx].userZoom = 1.0;

            viewStates[idx].center = QPointF(0.5, 0.5);

            presentImage(viewList[idx], &viewStates[idx]);

            viewFitBtns[idx]->setEnabled(false);

        });

        QObject::connect(btnOpen, &QToolButton::clicked, btnOpen,

                         [&]() { openInspect(false, 0); });

    }

    // ---------------- 右侧：计数 + 列表 + 详情卡 ----------------

    auto *right = new QFrame;

    right->setObjectName("card");

    // 同上：右检视栏的可调范围也放宽，窄能窄到只露表格，宽能宽到看清长状态词

    right->setMinimumWidth(320);

    right->setMaximumWidth(780);

    auto *rightOuter = new QVBoxLayout(right);

    rightOuter->setContentsMargins(6, 6, 6, 6);

    auto *rightScroll = new QScrollArea;

    rightScroll->setWidgetResizable(true);

    rightScroll->setFrameShape(QFrame::NoFrame);

    // 拖窄时先让内容自己压缩；实在放不下再给横向滚动条，

    // 总比内容被卡片边缘直接裁掉、字看不见也滚不到强

    rightScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);

    auto *rightBody = new QWidget;

    rightScroll->setWidget(rightBody);

    rightOuter->addWidget(rightScroll);

    auto *rl = new QVBoxLayout(rightBody);

    rl->setContentsMargins(10, 8, 10, 8);

    rl->setSpacing(8);

    // 「检测结果」入口跟着结果走：载入 / 导出 / 帧切换放在右栏最上面

    rl->addWidget(phenotypingBox);

    body->addWidget(right);

    // 中栏吃满富余宽度；活动栏/侧栏已经移到页签外面，这里只剩视图区 + 右检视栏

    body->setStretchFactor(0, 1);

    body->setSizes({1000, 392});

    // 计数与一致性检查放同一行：省一行高度给表格

    auto *countRow = new QHBoxLayout;

    auto *count = new QLabel("--");

    count->setObjectName("big");

    auto *countTag = new QLabel("目标数量");

    countTag->setObjectName("detailKey");

    countRow->addWidget(countTag);

    countRow->addWidget(count);

    countRow->addStretch();

    rl->addLayout(countRow);

    // 一致性检查单独一行（右栏空间富余，宁可多一行也不要让字挤成两行）

    auto *consistency = new QLabel("尚未载入对象");

    consistency->setObjectName("consistency");

    // 允许折行：这行会带「计数一致 ✓」之类的长文本，钉成单行会把右栏最小宽度撑大

    consistency->setWordWrap(true);

    rl->addWidget(consistency);

    auto *modelState = new QLabel("模型状态 --");

    auto *inferLabel = new QLabel("推理耗时 -- ms");

    auto *modelRow = new QHBoxLayout;

    modelRow->addWidget(modelState);

    modelRow->addStretch();

    modelRow->addWidget(inferLabel);

    rl->addLayout(modelRow);

    // 单位写在标题上，表头用短词（出现滚动条时列宽变窄，长表头会被截）

    auto *listCaption = new QLabel("目标列表（长度 px · 面积 px²）");

    listCaption->setObjectName("caption");

    rl->addWidget(listCaption);

    auto *table = new QTableWidget(0, 5);

    // 中文短表头：英文字段名在 320px 的检视栏里会被列宽切掉半个词

    // （曾经出现表头只剩 "_ength""A"）。完整字段名在下面的「单粒详情」里。

    table->setHorizontalHeaderLabels({"Seed", "Conf", "长(px)", "宽(px)", "面积"});

    table->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);

    table->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);

    table->horizontalHeader()->setMinimumSectionSize(38);

    table->setFont(QFont("Microsoft YaHei UI", 9));

    table->verticalHeader()->setVisible(false);

    table->verticalHeader()->setDefaultSectionSize(22);

    table->setEditTriggers(QAbstractItemView::NoEditTriggers);

    table->setSelectionBehavior(QAbstractItemView::SelectRows);

    table->setSelectionMode(QAbstractItemView::SingleSelection);

    table->setAlternatingRowColors(true);

    table->setMinimumHeight(200);

    rl->addWidget(table, 1);

    auto *detailCaption = new QLabel("单粒详情");

    detailCaption->setObjectName("caption");

    rl->addWidget(detailCaption);

    auto *detailFrame = new QFrame;

    auto *dl = new QGridLayout(detailFrame);

    dl->setContentsMargins(0, 0, 0, 0);

    dl->setVerticalSpacing(3);

    const QStringList detailKeys = {"Area (px²)", "Length (px)", "Width (px)",

                                    "Perimeter (px)", "Aspect Ratio (-)",

                                    "Circularity (-)"};

    QVector<QLabel *> detailValues;

    for (int i = 0; i < detailKeys.size(); ++i) {

        auto *k = new QLabel(detailKeys[i]);

        k->setObjectName("detailKey");

        auto *v = new QLabel("—");

        v->setObjectName("detailValue");

        v->setAlignment(Qt::AlignRight | Qt::AlignVCenter);

        dl->addWidget(k, i, 0);

        dl->addWidget(v, i, 1);

        detailValues.append(v);

    }

    auto *statusKey = new QLabel("Status");

    statusKey->setObjectName("detailKey");

    auto *statusBadge = new QLabel("无选中");

    statusBadge->setAlignment(Qt::AlignCenter);

    statusBadge->setStyleSheet("background:#9aa995;color:white;border-radius:6px;"

                               "padding:4px 8px;font-weight:700;");

    dl->addWidget(statusKey, detailKeys.size(), 0);

    dl->addWidget(statusBadge, detailKeys.size(), 1);

    dl->setColumnStretch(1, 1);

    rl->addWidget(detailFrame);

    // B 给的 status_reason 直接显示（不阻塞：异常目标照常可选中、可导出）

    auto *reason = new QLabel;

    reason->setObjectName("detailKey");

    reason->setWordWrap(true);

    rl->addWidget(reason);

    // 说明性文字：默认收起，点标题才看

    auto *noteLay = makeCollapsible(rl, "单位与字段说明", uiExpand);

    auto *note = new QLabel(

        "未标定：Length/Width/Perimeter 为 px，Area 为 px²\n"

        "AR = Length/Width，Circularity = 4πA/P²，均无量纲\n"

        "「目标检测」「单粒表型」视图里可点击目标，与左侧列表联动");

    note->setObjectName("detailKey");

    note->setWordWrap(true);

    noteLay->addWidget(note);

    // 共用底部输出面板：挂在页签下方，单粒表型与对比两页都能看到日志。

    // 默认收起，省下的高度给视图（--ui-expand 时默认展开）；拖上面的分割条可改高度。

    auto *logPanel = new QFrame;

    logPanel->setObjectName("outputPanel");

    auto *logPanelLay = new QVBoxLayout(logPanel);

    logPanelLay->setContentsMargins(8, 4, 8, 4);

    logPanelLay->setSpacing(0);

    auto *logBox = makeCollapsible(logPanelLay, "输出 · 运行日志", uiExpand,

                                   "点标题展开/收起；拖它上面的分割条可改高度");

    auto *log = new QPlainTextEdit;

    log->setReadOnly(true);

    log->setMaximumBlockCount(300);

    log->setMinimumHeight(80);   // 只留下限，上限交给分割条

    logBox->addWidget(log);

    // 三栏作为「单粒表型测量」页，逻辑一字节都不改

    auto *phenotypingPage = new QWidget;

    auto *d5v = new QVBoxLayout(phenotypingPage);

    d5v->setContentsMargins(0, 0, 0, 0);

    d5v->setSpacing(8);

    d5v->addWidget(status);

    d5v->addWidget(metrics);

    d5v->addWidget(body, 1);

    // ===================== 粘连分离对比页 =====================

    // 三栏：原图 + 检测框 | 改进分离轮廓 | 指标卡

    // 图像选择收进顶部工具条；两个视图吃余量、指标卡保持可读字号

    auto *cmpPage = new QWidget;

    auto *cmpOuter = new QVBoxLayout(cmpPage);

    cmpOuter->setContentsMargins(0, 8, 0, 0);

    cmpOuter->setSpacing(8);

    // 模拟数据横幅：任务书 C1「明确标记模拟数据」，截图/录像里必然可见

    auto *mockBanner = new QFrame;

    mockBanner->setObjectName("mockBanner");

    auto *mockLay = new QHBoxLayout(mockBanner);

    mockLay->setContentsMargins(12, 7, 12, 7);

    auto *mockText = new QLabel("⚠ 模拟数据 MOCK");

    mockText->setStyleSheet("font-size:16px;");

    mockLay->addWidget(mockText);

    auto *mockNote = new QLabel;

    mockNote->setWordWrap(true);

    mockNote->setStyleSheet("font-weight:400; color:#7a5b13;");

    mockLay->addWidget(mockNote, 1);

    mockBanner->setVisible(false);       // 载入 real 数据时自动隐藏

    cmpOuter->addWidget(mockBanner);

    auto *cmpSplit = new QSplitter(Qt::Horizontal);

    cmpSplit->setChildrenCollapsible(false);

    cmpSplit->setHandleWidth(16);

    // ---- 栏 1：原图 + YOLO bbox ----

    auto *cmpLeftCard = new QFrame;

    cmpLeftCard->setObjectName("card");

    cmpLeftCard->setMinimumWidth(180);

    auto *cmpLeftLay = new QVBoxLayout(cmpLeftCard);

    cmpLeftLay->setContentsMargins(10, 10, 10, 10);

    cmpLeftLay->setSpacing(6);

    auto *cmpLeftCap = new QLabel("① 检测框");

    cmpLeftCap->setObjectName("caption");

    // 窄窗口下标题会被卡片边缘切掉最后一个字，允许折行 —— 1920 下宽度够，不会折

    cmpLeftCap->setWordWrap(true);

    cmpLeftCap->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);

    auto *cmpLeftView = new ClickableLabel;

    cmpLeftView->setObjectName("view");

    cmpLeftView->setAlignment(Qt::AlignCenter);

    cmpLeftView->setMinimumSize(200, 150);

    auto *cmpLeftFit = new QToolButton;

    cmpLeftFit->setObjectName("viewTool");

    cmpLeftFit->setText("适应");

    cmpLeftFit->setToolTip("缩放归位到「整张图刚好放得下」");

    cmpLeftFit->setEnabled(false);

    auto *cmpLeftOpen = new QToolButton;

    cmpLeftOpen->setObjectName("viewTool");

    cmpLeftOpen->setText("⤢");

    cmpLeftOpen->setToolTip("放大查看两路对比（双击图面同效）");

    {

        auto *capRow = new QHBoxLayout;

        capRow->setSpacing(4);

        capRow->addWidget(cmpLeftCap, 1);

        capRow->addWidget(cmpLeftFit, 0, Qt::AlignTop);

        capRow->addWidget(cmpLeftOpen, 0, Qt::AlignTop);

        cmpLeftLay->addLayout(capRow);

    }

    cmpLeftLay->addWidget(cmpLeftView, 1);

    cmpLeftView->onWheel = [&](QPoint p, int d) {

        zoomAt(cmpLeftView, &cmpLeftState, p, d);

        cmpLeftFit->setEnabled(cmpLeftState.userZoom > 1.001);

    };

    cmpLeftView->onDrag = [&](QPoint delta) {

        if (cmpLeftState.userZoom <= 1.001) return;

        const QRect cr = cmpLeftView->contentsRect();

        const double w = cmpLeftState.composite.width() * cmpLeftState.scale;

        const double h = cmpLeftState.composite.height() * cmpLeftState.scale;

        if (w > cr.width())

            cmpLeftState.center.setX(

                qBound(0.0, cmpLeftState.center.x() - delta.x() / w, 1.0));

        if (h > cr.height())

            cmpLeftState.center.setY(

                qBound(0.0, cmpLeftState.center.y() - delta.y() / h, 1.0));

        presentImage(cmpLeftView, &cmpLeftState);

    };

    cmpLeftView->onDoubleClicked = [&]() { openInspect(true, 0); };

    cmpLeftView->onResized = [&]() {

        presentImage(cmpLeftView, &cmpLeftState);

        cmpLeftFit->setEnabled(cmpLeftState.userZoom > 1.001);

    };

    QObject::connect(cmpLeftFit, &QToolButton::clicked, cmpLeftFit, [&]() {

        cmpLeftState.userZoom = 1.0;

        cmpLeftState.center = QPointF(0.5, 0.5);

        presentImage(cmpLeftView, &cmpLeftState);

        cmpLeftFit->setEnabled(false);

    });

    QObject::connect(cmpLeftOpen, &QToolButton::clicked, cmpLeftOpen,

                     [&]() { openInspect(true, 0); });

    // ---- 栏 2：改进分离实例轮廓 ----

    auto *cmpRightCard = new QFrame;

    cmpRightCard->setObjectName("card");

    cmpRightCard->setMinimumWidth(180);

    auto *cmpRightLay = new QVBoxLayout(cmpRightCard);

    cmpRightLay->setContentsMargins(10, 10, 10, 10);

    cmpRightLay->setSpacing(6);

    auto *cmpRightCap = new QLabel("② 分离轮廓");

    cmpRightCap->setObjectName("caption");

    cmpRightCap->setWordWrap(true);

    cmpRightCap->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);

    auto *cmpRightView = new ClickableLabel;

    cmpRightView->setObjectName("view");

    cmpRightView->setAlignment(Qt::AlignCenter);

    cmpRightView->setMinimumSize(200, 150);

    auto *cmpRightFit = new QToolButton;

    cmpRightFit->setObjectName("viewTool");

    cmpRightFit->setText("适应");

    cmpRightFit->setToolTip("缩放归位到「整张图刚好放得下」");

    cmpRightFit->setEnabled(false);

    auto *cmpRightOpen = new QToolButton;

    cmpRightOpen->setObjectName("viewTool");

    cmpRightOpen->setText("⤢");

    cmpRightOpen->setToolTip("放大查看两路对比（双击图面同效）");

    {

        auto *capRow = new QHBoxLayout;

        capRow->setSpacing(4);

        capRow->addWidget(cmpRightCap, 1);

        capRow->addWidget(cmpRightFit, 0, Qt::AlignTop);

        capRow->addWidget(cmpRightOpen, 0, Qt::AlignTop);

        cmpRightLay->addLayout(capRow);

    }

    cmpRightLay->addWidget(cmpRightView, 1);

    cmpRightView->onWheel = [&](QPoint p, int d) {

        zoomAt(cmpRightView, &cmpRightState, p, d);

        cmpRightFit->setEnabled(cmpRightState.userZoom > 1.001);

    };

    cmpRightView->onDrag = [&](QPoint delta) {

        if (cmpRightState.userZoom <= 1.001) return;

        const QRect cr = cmpRightView->contentsRect();

        const double w = cmpRightState.composite.width() * cmpRightState.scale;

        const double h = cmpRightState.composite.height() * cmpRightState.scale;

        if (w > cr.width())

            cmpRightState.center.setX(

                qBound(0.0, cmpRightState.center.x() - delta.x() / w, 1.0));

        if (h > cr.height())

            cmpRightState.center.setY(

                qBound(0.0, cmpRightState.center.y() - delta.y() / h, 1.0));

        presentImage(cmpRightView, &cmpRightState);

    };

    cmpRightView->onDoubleClicked = [&]() { openInspect(true, 1); };

    cmpRightView->onResized = [&]() {

        presentImage(cmpRightView, &cmpRightState);

        cmpRightFit->setEnabled(cmpRightState.userZoom > 1.001);

    };

    QObject::connect(cmpRightFit, &QToolButton::clicked, cmpRightFit, [&]() {

        cmpRightState.userZoom = 1.0;

        cmpRightState.center = QPointF(0.5, 0.5);

        presentImage(cmpRightView, &cmpRightState);

        cmpRightFit->setEnabled(false);

    });

    QObject::connect(cmpRightOpen, &QToolButton::clicked, cmpRightOpen,

                     [&]() { openInspect(true, 0); });

    // ---- 栏 3：指标卡 ----

    auto *cmpPanel = new QFrame;

    cmpPanel->setObjectName("card");

    // 300 是「分离模式」两个按钮 + 三个大数字仍能完整显示的下限；

    // 拖分割条时到这条线就停住，不再让内容被卡片边缘切掉

    cmpPanel->setMinimumWidth(300);

    cmpPanel->setMaximumWidth(720);

    auto *cmpPanelOuter = new QVBoxLayout(cmpPanel);

    cmpPanelOuter->setContentsMargins(10, 10, 10, 10);

    cmpPanelOuter->setSpacing(6);

    auto *cmpPanelScroll = new QScrollArea;

    cmpPanelScroll->setWidgetResizable(true);

    cmpPanelScroll->setFrameShape(QFrame::NoFrame);

    cmpPanelScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);

    auto *cmpPanelBody = new QWidget;

    auto *cp = new QVBoxLayout(cmpPanelBody);

    cp->setContentsMargins(2, 2, 2, 2);

    cp->setSpacing(8);

    cmpPanelScroll->setWidget(cmpPanelBody);

    cmpPanelOuter->addWidget(cmpPanelScroll, 1);

    // 分离模式切换：固定分水岭 / 自适应

    auto *cmpModeRow = new QHBoxLayout;

    cmpModeRow->setSpacing(6);

    auto *cmpModeTitle = new QLabel("分离模式");

    cmpModeTitle->setObjectName("detailKey");

    cmpModeRow->addWidget(cmpModeTitle);

    auto *cmpBtnFixed = new QPushButton("固定分水岭");

    auto *cmpBtnAdaptive = new QPushButton("自适应");

    for (QPushButton *b : {cmpBtnFixed, cmpBtnAdaptive}) {

        b->setObjectName("cmpMode");

        b->setCheckable(true);

        b->setMinimumHeight(34);

        cmpModeRow->addWidget(b, 1);

    }

    cmpBtnAdaptive->setChecked(true);

    auto *cmpModeGroup = new QButtonGroup(&win);

    cmpModeGroup->setExclusive(true);

    cmpModeGroup->addButton(cmpBtnFixed, 1);

    cmpModeGroup->addButton(cmpBtnAdaptive, 2);

    cp->addLayout(cmpModeRow);

    // 三个大数字：校赛投屏时最该看清的就是这三个

    auto *cmpBigRow = new QHBoxLayout;

    cmpBigRow->setSpacing(6);

    QLabel *cmpBigVal[3];

    const char *cmpBigCapText[3] = {"YOLO 数量", "改进数量", "绝对误差"};

    for (int i = 0; i < 3; ++i) {

        auto *cell = new QFrame;

        cell->setObjectName("ai");

        cell->setMinimumWidth(0);

        auto *cl = new QVBoxLayout(cell);

        cl->setContentsMargins(6, 8, 6, 8);

        cl->setSpacing(1);

        auto *v = new QLabel("--");

        v->setObjectName("cmpBig");

        v->setAlignment(Qt::AlignCenter);

        // 三个格子等分面板宽度；字不许反过来撑宽卡片

        v->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);

        auto *k = new QLabel(cmpBigCapText[i]);

        k->setObjectName("cmpBigCap");

        k->setAlignment(Qt::AlignCenter);

        k->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);

        cl->addWidget(v);

        cl->addWidget(k);

        cmpBigVal[i] = v;

        cmpBigRow->addWidget(cell, 1);

    }

    cp->addLayout(cmpBigRow);

    // 指标明细：Species / GT / 分离耗时 / 当前图

    auto *cmpInfoGrid = new QGridLayout;

    cmpInfoGrid->setHorizontalSpacing(10);

    cmpInfoGrid->setVerticalSpacing(4);

    const char *cmpInfoKeyText[4] = {"SPECIES", "GT 真实粒数", "分离耗时", "IMAGE ID"};

    QLabel *cmpInfoVal[4];

    for (int i = 0; i < 4; ++i) {

        auto *k = new QLabel(cmpInfoKeyText[i]);

        k->setObjectName("detailKey");

        auto *v = new QLabel("--");

        v->setObjectName("detailValue");

        v->setWordWrap(true);

        // IMAGE ID 这类值没有空格，长度一长就会把整张指标卡撑到视口外，

        // 看起来像「分割条把字盖住了」。横向 Ignored：面板给多宽就用多宽。

        v->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);

        v->setMinimumWidth(0);

        if (i < 3) {

            cmpInfoGrid->addWidget(k, i / 2, (i % 2) * 2);

            cmpInfoGrid->addWidget(v, i / 2, (i % 2) * 2 + 1);

        } else {

            // IMAGE ID 单独占一整行，别和「分离耗时」挤半栏

            cmpInfoGrid->addWidget(k, 2, 0);

            cmpInfoGrid->addWidget(v, 2, 1, 1, 3);

        }

        cmpInfoVal[i] = v;

    }

    cmpInfoGrid->setColumnStretch(1, 1);

    cmpInfoGrid->setColumnStretch(3, 1);

    cp->addLayout(cmpInfoGrid);

    auto *cmpGtNote = new QLabel;

    cmpGtNote->setObjectName("hint");

    cmpGtNote->setWordWrap(true);

    cmpGtNote->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);

    cp->addWidget(cmpGtNote);

    // 耗时：四段分开，缺测写 N/A（任务书 C2 硬要求）

    auto *cmpTimeTitle = new QLabel("耗时（缺测显示 N/A）");

    cmpTimeTitle->setObjectName("cmpTitle");

    cp->addWidget(cmpTimeTitle);

    auto *cmpTimeGrid = new QGridLayout;

    cmpTimeGrid->setHorizontalSpacing(10);

    cmpTimeGrid->setVerticalSpacing(3);

    const char *cmpTimeKeyText[4] = {"YOLO 推理", "分离", "FPGA / DMA", "整链路"};

    QLabel *cmpTimeVal[4];

    for (int i = 0; i < 4; ++i) {

        auto *k = new QLabel(cmpTimeKeyText[i]);

        k->setObjectName("detailKey");

        auto *v = new QLabel("N/A");

        v->setObjectName("cmpNA");

        cmpTimeGrid->addWidget(k, i, 0);

        cmpTimeGrid->addWidget(v, i, 1);

        cmpTimeVal[i] = v;

    }

    cmpTimeGrid->setColumnStretch(1, 1);

    cp->addLayout(cmpTimeGrid);

    // 实例列表

    auto *cmpObjTitle = new QLabel("分离实例（面积 px²）");

    cmpObjTitle->setObjectName("cmpTitle");

    cp->addWidget(cmpObjTitle);

    auto *cmpTable = new QTableWidget(0, 4);

    cmpTable->setHorizontalHeaderLabels({"Seed ID", "状态", "来源框", "面积"});

    cmpTable->verticalHeader()->setVisible(false);

    cmpTable->verticalHeader()->setDefaultSectionSize(22);

    // 状态列会出 BROKEN_CONTOUR 这种长词。列宽策略：状态列吃剩余宽度（长词省略、

    // 悬停看全称），其余三列固定 —— 这样任何面板宽度下「面积」都不会被挤出卡片。

    // 横向策略 Ignored + 关横向滚动条：面板比表格 sizeHint 窄时也不把内容顶出去。

    cmpTable->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

    cmpTable->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Expanding);

    cmpTable->horizontalHeader()->setMinimumSectionSize(36);

    cmpTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Interactive);

    cmpTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Stretch);

    cmpTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Interactive);

    cmpTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::Interactive);

    // 四列固定宽之和 + 竖向滚动条要小于指标卡最小内宽（~270），

    // 否则最右「面积」列会被表格视口切掉

    cmpTable->setColumnWidth(0, 76);

    cmpTable->setColumnWidth(2, 56);

    cmpTable->setColumnWidth(3, 64);

    cmpTable->setSelectionBehavior(QAbstractItemView::SelectRows);

    cmpTable->setSelectionMode(QAbstractItemView::SingleSelection);

    cmpTable->setEditTriggers(QAbstractItemView::NoEditTriggers);

    cmpTable->setMinimumHeight(150);

    cp->addWidget(cmpTable, 1);

    // 失败原因原文（有才显示）

    auto *cmpFailNote = new QLabel;

    cmpFailNote->setObjectName("detailKey");

    cmpFailNote->setWordWrap(true);

    cmpFailNote->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);

    cmpFailNote->setVisible(false);

    cp->addWidget(cmpFailNote);

    cp->addStretch(0);

    // ---- 对比数据工具条：载入 / 选图 / 筛选 ----

    // 这几项原来在侧栏「对比」页；侧栏现在只控制左侧栏本身、不随页签变，

    // 所以把入口放回对比页自身。图像用下拉代替整列列表，给两张对比图省出宽度。

    auto *cmpBar = new QFrame;

    auto *cmpBarLay = new QHBoxLayout(cmpBar);

    cmpBarLay->setContentsMargins(0, 0, 0, 0);

    cmpBarLay->setSpacing(6);

    auto *cmpLoad = new QPushButton("⇩ 载入对比结果…");

    auto *cmpLoadDemo = new QPushButton("演示数据");

    auto *cmpGenerate = new QPushButton("生成对比…");

    for (QPushButton *b : {cmpLoad, cmpLoadDemo, cmpGenerate})

        b->setCursor(Qt::PointingHandCursor);

    cmpLoad->setToolTip("选择已有的对比 JSON（顶层 images[]）");

    cmpLoadDemo->setToolTip("载入随程序交付的演示结果（真实对比数据，三品种各 5 张）；"

                            "缺失时才用模拟数据");

    cmpGenerate->setToolTip("用当前输入（单张图片 / 批量目录 / 最新实时帧）现场跑一遍"

                            "检测与分离，生成结果并载入本页；可勾选先经板端处理");

    cmpBarLay->addWidget(cmpLoad);

    cmpBarLay->addWidget(cmpLoadDemo);

    cmpBarLay->addWidget(cmpGenerate);

    cmpBarLay->addSpacing(6);

    auto *cmpPickLabel = new QLabel("图像");

    cmpBarLay->addWidget(cmpPickLabel);

    auto *cmpCombo = new QComboBox;

    cmpCombo->setMinimumWidth(180);

    cmpCombo->setMaximumWidth(340);      // 再宽就挤掉「只看失败案例」

    cmpCombo->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);

    cmpCombo->setToolTip("选当前对比图；失败案例以 ! 开头，悬停看每路数量与真值");

    cmpBarLay->addWidget(cmpCombo, 1);

    auto *cmpOnlyFail = new QCheckBox("只看失败案例");

    cmpOnlyFail->setToolTip("筛出 status = fail 的图：错误拆分 / 漏分 / 背景噪声等真实问题");

    cmpBarLay->addWidget(cmpOnlyFail);

    auto *cmpListStat = new QLabel("--");

    cmpListStat->setObjectName("hint");

    cmpListStat->setWordWrap(false);

    cmpOuter->addWidget(cmpBar);

    cmpSplit->addWidget(cmpLeftCard);

    cmpSplit->addWidget(cmpRightCard);

    cmpSplit->addWidget(cmpPanel);

    cmpSplit->setStretchFactor(0, 1);

    cmpSplit->setStretchFactor(1, 1);

    cmpSplit->setStretchFactor(2, 0);

    cmpSplit->setSizes({560, 560, 330});   // 默认把余量多给两张对比图

    cmpOuter->addWidget(cmpSplit, 1);

    // 翻页条

    auto *cmpNav = new QHBoxLayout;

    cmpNav->setSpacing(6);

    auto *cmpPrev = new QPushButton("◀ 上一张");

    auto *cmpNext = new QPushButton("下一张 ▶");

    cmpPrev->setObjectName("cmpStep");

    cmpNext->setObjectName("cmpStep");

    cmpNav->addWidget(cmpPrev);

    cmpNav->addWidget(cmpNext);

    // 统计放翻页条：工具条右端给「只看失败」留出空间，窄窗也不会把字切掉

    cmpNav->addWidget(cmpListStat);

    cmpNav->addStretch();

    auto *cmpHint = new QLabel(

        "点轮廓 / 实例行可双向选中；滚轮放大、按住拖动平移；双击视图开大窗对比");

    cmpHint->setObjectName("hint");

    cmpHint->setWordWrap(true);

    cmpNav->addWidget(cmpHint, 1);

    cmpOuter->addLayout(cmpNav);

    // 滚轮统一给面板：否则鼠标停在「数据源」「分辨率」「置信度」上滚轮是改它们的值，

    // 停在空白处滚才是翻面板 —— 同一个面板两种行为，演示时很容易滚错

    for (const auto &pg : sidePages) guardWheel(pg.first, pg.second);

    guardWheel(rightBody, rightScroll);

    guardWheel(cmpPanelBody, cmpPanelScroll);

    auto *tabs = new QTabWidget;

    tabs->addTab(phenotypingPage, "单粒表型测量");

    tabs->addTab(cmpPage, "粘连分离对比");

    // 运行按钮挂在页签栏右上角：切到哪一页都在同一个位置，不用回侧栏找

    auto *runBar = new QWidget;

    auto *runBarLay = new QHBoxLayout(runBar);

    runBarLay->setContentsMargins(0, 0, 4, 6);

    runBarLay->setSpacing(6);

    for (QPushButton *b : {start, stop, restart, netConnect, netDisconnect}) {

        b->setMinimumHeight(30);

        b->setMaximumHeight(30);

        b->setCursor(Qt::PointingHandCursor);

        runBarLay->addWidget(b);

    }

    tabs->setCornerWidget(runBar, Qt::TopRightCorner);

    // 工作区 = 页签 + 共用底部输出面板（竖向分割条可拖高度）

    auto *workspaceSplit = new QSplitter(Qt::Vertical);

    workspaceSplit->setChildrenCollapsible(false);

    workspaceSplit->setHandleWidth(14);

    workspaceSplit->addWidget(tabs);

    workspaceSplit->addWidget(logPanel);

    workspaceSplit->setStretchFactor(0, 1);

    workspaceSplit->setSizes({uiExpand ? 700 : 860, uiExpand ? 180 : 42});

    // 最外层：活动栏/侧栏 | 工作区。侧栏两页共用、高度贯穿到底，

    // 与编辑器一致；底部输出面板只占工作区宽度。

    auto *outerSplit = new QSplitter(Qt::Horizontal);

    outerSplit->setChildrenCollapsible(false);

    outerSplit->setHandleWidth(16);

    outerSplit->addWidget(sideWrap);

    outerSplit->addWidget(workspaceSplit);

    outerSplit->setStretchFactor(0, 0);

    outerSplit->setStretchFactor(1, 1);

    outerSplit->setSizes({400, 1400});

    vl->addWidget(outerSplit, 1);

    // 活动栏只切左侧长栏，**不动右边的工作区页签**：

    //   · 点另一个图标 → 换侧栏页并展开

    //   · 再点当前图标 → 收起侧栏；收起后再点任一图标 → 展开

    // 右边看哪一页由页签自己决定，两套操作互不干扰。

    int activeSection = 0;

    bool sideCollapsed = false;

    int sideRestoreWidth = 400;

    auto setSideShown = [&](bool shown) {

        if (shown == !sideCollapsed) return;

        if (!shown) {

            sideRestoreWidth = qMax(260, sideWrap->width());

            sideCard->hide();

            sideCollapsed = true;

            // 只藏 sideCard 的话，splitter 里 sideWrap 仍占着原宽度，会留一条空白；

            // 把最外层左格一起收到活动栏宽度，工作区才能真正拿到空间

            const int total = outerSplit->width();

            const int actW = activity->width() > 0 ? activity->width() : 42;

            outerSplit->setSizes({actW, qMax(1, total - actW)});

        } else {

            sideCollapsed = false;

            sideCard->show();

            const int total = outerSplit->width();

            const int left = qBound(240, sideRestoreWidth, qMax(240, total - 360));

            outerSplit->setSizes({left, qMax(1, total - left)});

        }

    };

    auto switchSection = [&](int i) {

        activeSection = i;

        actBtns[i]->setChecked(true);

        sideStack->setCurrentIndex(i);

        setSideShown(true);

    };

    for (int i = 0; i < actBtns.size(); ++i)

        QObject::connect(actBtns[i], &QToolButton::clicked, actBtns[i], [&, i]() {

            if (!sideCollapsed && activeSection == i) {

                setSideShown(false);         // 再点一次收起

                return;

            }

            switchSection(i);

        });

    switchSection(0);

    if (cli.isSet(optSection))

        switchSection(qBound(0, cli.value(optSection).toInt(), actBtns.size() - 1));

    if (cli.isSet(optSide)) setSideShown(false);   // 自检：截图用收起态

    // ===================== 运行时状态 =====================

    QProcess process;

    QProcess genProcess;              // 分析/生成：独立进程，不打断采集

    QString genOutPath;               // 本轮生成实际写入的对比 JSON（对话框可改路径）

    QString genPhenoPath;             // 非空 = 本轮同时产出单粒表型 JSON

    bool genWantPheno = true;         // 本轮是否载入表型结果

    bool genWantCmp = true;           // 本轮是否载入对比结果

    bool genLive = false;             // true = 实时自动分析，结束后不切页、不刷日志

    bool genSwitchTab = true;         // 产出后是否切到对比页（自动分析不切）

    bool genQuiet = false;            // 载入失败是否弹窗（自动分析只写日志）

    bool genBusyOwner = false;        // busy 是否由本进程持有（结束时负责解锁）

    QString genTag = "生成";          // 日志前缀：生成 / 分析 / 实时分析

    std::function<void(bool)> setBusyFn;   // setBusy 在后端段定义，这里先留名字

    QTimer poll, captureWatch, liveAnalyze;

    poll.setInterval(200);

    captureWatch.setInterval(300);

    // 实时自动分析：上一轮分析结束后再取最新帧，间隔只决定「等多久再看一眼」，

    // 不是固定帧率。分析本身在 PC 上跑冻结检测器，比采集慢，不会被采集拖住。

    liveAnalyze.setInterval(2000);

    bool liveShowAnalysis = false;    // true = 表面型结果，暂停 live 检测覆盖

    qint64 liveAnalyzedStamp = -1;    // 已分析过的预览帧（mtime+size）

    bool liveResultLogged = false;    // 实时分析只报一次「首轮已载入」

    std::function<void()> liveAnalyzeOnce;   // --analyze-live 自检直接调它

    QVector<FrameData> frames;        // B 的多帧结果，或 live 的单帧

    int currentFrame = -1;

    QVector<SeedObject> objects;      // = frames[currentFrame].objects（引用语义的浅拷贝）

    int selectedObj = -1;

    int batchSeq = 0;

    bool selectionSync = false;

    bool busy = false;

    bool showLive = false;            // true=显示 live 检测结果；false=显示载入的表型结果

    bool captureDone = false;

    QString loadedJson;

    QString runDir = outDirEdit->text();

    QByteArray output;

    bool stopping = false;

    bool pendingRestart = false;

    int backendCrashRestarts = 0;

    QElapsedTimer captureClock;

    int lastLiveFrame = -1;

    QString lastLiveSignature;

    bool liveNeedsRebuild = true;      // 启动后第一次轮询必须重建，哪怕检测结果为空

    // ---------------- segmentation Comparison 运行时状态 ----------------

    QVector<CmpImage> cmpImages;

    QString cmpSchema, cmpSource, cmpSourceNote, cmpModeNote;

    QString cmpModeLabel[3] = {"原 YOLO", "固定分水岭", "自适应"};

    double cmpChainFpga = -1, cmpChainTotal = -1;   // 整批级链路耗时，-1 = 缺测

    int cmpIndex = -1;

    // cmpMode 既是按钮 id 也是 ci.modes[] 的下标：0=yolo 1=fixed 2=adaptive。

    // 曾经写成 modes[cmpMode - 1]，结果「自适应」显示的是固定分水岭的数 —— 别再加 -1。

    int cmpMode = 2;                 // 1 = fixed, 2 = adaptive

    int cmpSelected = -1;

    QString cmpLoadedPath;

    QVector<int> cmpListMap;         // 列表行号 -> cmpImages 下标（"只看失败案例"过滤后）

    // 先声明、后赋值：载入/绘制/列表之间互相调用，必须先有名字

    std::function<void()> redraw;

    std::function<void()> pollStatus;   // 后端退出时补读一次最终 live_status.json

    std::function<void()> fillTable;

    std::function<void()> updateConsistency;

    std::function<void()> updateDetail;

    std::function<void(int)> selectObject;

    std::function<QString(const QString &, int)> exportBatchTo;

    std::function<void()> launch;

    auto append = [&](const QString &s) {

        log->appendPlainText(QTime::currentTime().toString("HH:mm:ss") + "  " + s);

    };

    auto current = [&]() -> FrameData * {

        if (currentFrame < 0 || currentFrame >= frames.size()) return nullptr;

        return &frames[currentFrame];

    };

    // ---------------- 解析 ----------------

    // 单目标：兼容 B 的 bbox_xyxy_src / seed_id 与契约的 bbox

    auto parseObject = [&](const QJsonObject &o, int idx, SeedObject *out) -> QString {

        static const char *required[] = {

            "id", "confidence", "area_px", "length_px", "width_px",

            "perimeter_px", "aspect_ratio", "circularity", "status"};

        for (const char *key : required) {

            if (!o.contains(QLatin1String(key)))

                return QString("objects[%1] 缺少字段 %2").arg(idx).arg(QLatin1String(key));

        }

        const QJsonArray bb = o.contains("bbox_xyxy_src")

            ? o.value("bbox_xyxy_src").toArray() : o.value("bbox").toArray();

        if (bb.size() != 4)

            return QString("objects[%1] 的检测框必须为 [x1,y1,x2,y2]"

                           "（字段名 bbox_xyxy_src 或 bbox 二者之一）").arg(idx);

        out->id = o.value("id").toInt();

        out->seedIdText = o.value("seed_id").toString();

        out->confidence = o.value("confidence").toDouble();

        out->bbox = QRectF(bb[0].toDouble(), bb[1].toDouble(),

                           bb[2].toDouble() - bb[0].toDouble(),

                           bb[3].toDouble() - bb[1].toDouble());

        out->areaPx = o.value("area_px").toDouble();

        out->lengthPx = o.value("length_px").toDouble();

        out->widthPx = o.value("width_px").toDouble();

        out->perimeterPx = o.value("perimeter_px").toDouble();

        out->aspectRatio = o.value("aspect_ratio").toDouble();

        out->circularity = o.value("circularity").toDouble();

        out->status = o.value("status").toString();

        out->statusReason = o.value("status_reason").toString();

        out->hasMetrics = true;

        return QString();

    };

    auto parseObjects = [&](const QJsonArray &arr, QVector<SeedObject> *out) -> QString {

        int idx = 0;

        for (const QJsonValue &value : arr) {

            SeedObject s;

            const QString err = parseObject(value.toObject(), idx, &s);

            if (!err.isEmpty()) return err;

            out->append(s);

            ++idx;

        }

        QSet<int> seen;

        for (const SeedObject &s : *out) {

            if (seen.contains(s.id)) return QString("Seed ID 重复: %1").arg(s.id);

            seen.insert(s.id);

        }

        return QString();

    };

    // 载入：把 JSON 变成 frames[]（两种结构都认）

    // keepLive = true：实时自动分析的结果，载入后仍保持「采集运行中」，

    // 由调用方再用 liveShowAnalysis 暂停 live 检测覆盖；

    // 普通载入/手动分析则把 showLive 关掉，避免轮询把结果冲掉。

    auto loadJsonPath = [&](const QString &path, bool keepLive = false,

                            bool quiet = false) -> QString {

        QFile file(path);

        if (!file.open(QIODevice::ReadOnly))

            return "无法读取：" + file.errorString();

        QJsonParseError parse;

        const QJsonDocument doc = QJsonDocument::fromJson(file.readAll(), &parse);

        if (parse.error != QJsonParseError::NoError || !doc.isObject())

            return "JSON 无效：" + parse.errorString();

        const QJsonObject rootObj = doc.object();

        const QStringList imageDirs = buildImageDirs(path, imagesArg);

        QVector<FrameData> parsedFrames;

        if (rootObj.contains("frames")) {                 // A. B 的原生格式

            for (const QJsonValue &fv : rootObj.value("frames").toArray()) {

                const QJsonObject fo = fv.toObject();

                FrameData fd;

                fd.frameId = fo.value("frame_id").toInt(-1);

                fd.seedCount = fo.contains("seed_count")

                    ? fo.value("seed_count").toInt() : -1;

                const QString err = parseObjects(fo.value("objects").toArray(),

                                                 &fd.objects);

                if (!err.isEmpty()) return "对象结构不符：" + err;

                parsedFrames.append(fd);

            }

            if (parsedFrames.isEmpty())

                return "JSON 的 frames 数组为空";

        } else if (rootObj.contains("objects")) {          // B. C 侧契约

            FrameData fd;

            fd.frameId = rootObj.value("frame_id").toInt(-1);

            fd.seedCount = rootObj.contains("seed_count")

                ? rootObj.value("seed_count").toInt() : -1;

            const QString err = parseObjects(rootObj.value("objects").toArray(),

                                             &fd.objects);

            if (!err.isEmpty()) return "对象结构不符：" + err;

            parsedFrames.append(fd);

        } else {

            return "顶层既没有 frames 也没有 objects（多帧结果文件应有 frames 数组）";

        }

        // 每帧配原图与 PL 结果图

        for (FrameData &fd : parsedFrames) {

            const QString orig = frameImagePath(imageDirs, fd.frameId, false);

            const QString fpga = frameImagePath(imageDirs, fd.frameId, true);

            if (!orig.isEmpty()) {

                QImageReader reader(orig);

                reader.setAutoTransform(true);

                fd.original = reader.read();

                fd.imageDir = QFileInfo(orig).absolutePath();

            }

            if (!fpga.isEmpty()) {

                QImageReader reader(fpga);

                reader.setAutoTransform(true);

                fd.fpga = reader.read();

                if (fd.imageDir.isEmpty()) fd.imageDir = QFileInfo(fpga).absolutePath();

            }

        }

        frames = parsedFrames;

        loadedJson = path;

        showLive = keepLive;

        currentFrame = 0;

        frameBox->clear();

        for (const FrameData &fd : frames)

            frameBox->addItem(QString("帧 %1（%2 粒）")

                                  .arg(fd.frameId).arg(fd.objects.size()));

        frameBox->setEnabled(frames.size() > 1);

        frameBox->setCurrentIndex(0);

        objects = frames[0].objects;

        selectedObj = -1;

        fillTable();

        updateConsistency();

        updateDetail();

        if (!objects.isEmpty()) selectObject(0);

        redraw();

        save->setEnabled(true);

        const int nonOk = int(std::count_if(objects.begin(), objects.end(),

            [](const SeedObject &o) { return !o.status.isEmpty() && o.status != "OK"; }));

        if (!quiet) {

            append(QString("载入 %1：%2 帧，当前帧 %3 粒（非 OK %4 粒）")

                       .arg(QFileInfo(path).fileName())

                       .arg(frames.size()).arg(objects.size()).arg(nonOk));

            if (!frames[0].imageDir.isEmpty())

                append("帧图像已随检测结果载入");

            else

                append("警告：没找到这一帧的原图/PL 图，视图会留空（可用 --images 指定目录）");

        }

        status->setText(QString("已载入检测结果 · 帧 %1 · 目标 %2 · 单位 px / px²")

                            .arg(frames[0].frameId).arg(objects.size()));

        return QString();

    };

    // ---------------- 绘制 ----------------

    auto drawBoxes = [&](const QImage &base, bool exportMode) {

        QImage img = base.convertToFormat(QImage::Format_RGB32);

        if (objects.isEmpty()) return img;

        QPainter p(&img);

        p.setRenderHint(QPainter::Antialiasing, true);

        const int w = img.width();

        // 图上标注按源图坐标画、显示时整体缩放：字号要按缩放后的观感给足。

        // 上限不能太小：单张/批量是 3000+ px 的原图，缩到视图里只剩 1/5，

        // 以前封顶 34px 时标签缩完只有 6px，屏幕上根本看不清。

        const int fontPx = qBound(15, w / 22, 150);

        const int thin = qMax(2, w / 400);

        const int thick = qMax(3, w / 200);

        QFont font("Arial");

        font.setPixelSize(fontPx);

        font.setBold(true);

        p.setFont(font);

        QFontMetrics fm(font);

        // 目标多时标签会互相压住，反而看不见选中框：密集场景只给选中项画标签

        const bool dense = objects.size() > 15;

        for (int i = 0; i < objects.size(); ++i) {

            const SeedObject &o = objects[i];

            const bool sel = (i == selectedObj);

            const QColor color = statusColor(o.status);

            p.setPen(QPen(sel ? kSelectColor : color, sel ? thick : thin));

            p.setBrush(Qt::NoBrush);

            p.drawRect(o.bbox);

        }

        // 标签分两轮：先画可省的目标，选中项最后画（永远在最上层）

        QVector<int> order;

        for (int i = 0; i < objects.size(); ++i)

            if (i != selectedObj) order.append(i);

        if (selectedObj >= 0 && selectedObj < objects.size()) order.append(selectedObj);

        QVector<QRectF> used;

        for (int i : order) {

            const SeedObject &o = objects[i];

            const bool sel = (i == selectedObj);

            const QColor color = statusColor(o.status);

            QString text;

            if (sel) {

                text = displayTag(o) + QString("  c=%1").arg(o.confidence, 0, 'f', 2);

            } else if (dense) {

                text = exportMode ? QString("S%1").arg(o.id) : QString();

            } else {

                text = displayTag(o) + QString("  c=%1").arg(o.confidence, 0, 'f', 2);

            }

            if (text.isEmpty()) continue;

            const QRect tr = fm.boundingRect(text).adjusted(-4, -2, 4, 2);

            QRectF bg(o.bbox.x(), o.bbox.y() - tr.height() - 2, tr.width(), tr.height());

            if (bg.top() < 0) bg.moveTop(o.bbox.y() + 2);

            if (bg.x() < 0) bg.moveLeft(1);

            if (bg.right() > w) bg.moveRight(w - 1);

            // 自动避让：与已画标签相交的就不画了（否则字会糊成一片）

            bool clash = false;

            for (const QRectF &u : used)

                if (u.intersects(bg)) { clash = true; break; }

            if (clash && !sel) continue;

            used.append(bg);

            const QColor bgc = sel ? kSelectColor : color;

            p.fillRect(bg, QColor(bgc.red(), bgc.green(), bgc.blue(),

                                  sel || exportMode ? 210 : 150));

            p.setPen(Qt::white);

            p.drawText(bg, Qt::AlignCenter, text);

        }

        return img;

    };

    auto drawPheno = [&](const QImage &base) {

        QImage img = base.convertToFormat(QImage::Format_RGB32);

        if (objects.isEmpty()) return img;

        QPainter p(&img);

        p.setRenderHint(QPainter::Antialiasing, true);

        const FrameData *fd = current();

        const QImage *ref = (fd && !fd->original.isNull()) ? &fd->original : nullptr;

        const double fx = ref ? double(img.width()) / ref->width() : 1.0;

        const double fy = ref ? double(img.height()) / ref->height() : 1.0;

        const int w = img.width();

        // 图上标注按源图坐标画、显示时整体缩放：字号要按缩放后的观感给足。

        // 上限不能太小：单张/批量是 3000+ px 的原图，缩到视图里只剩 1/5，

        // 以前封顶 34px 时标签缩完只有 6px，屏幕上根本看不清。

        const int fontPx = qBound(15, w / 22, 150);

        const int thin = qMax(2, w / 400);

        const int thick = qMax(3, w / 200);

        QFont font("Arial");

        font.setPixelSize(fontPx);

        font.setBold(true);

        p.setFont(font);

        QFontMetrics fm(font);

        const bool dense = objects.size() > 15;

        QVector<QRectF> scaled(objects.size());

        for (int i = 0; i < objects.size(); ++i) {

            const SeedObject &o = objects[i];

            const bool sel = (i == selectedObj);

            const QColor color = statusColor(o.status);

            const QRectF box(o.bbox.x() * fx, o.bbox.y() * fy,

                             o.bbox.width() * fx, o.bbox.height() * fy);

            scaled[i] = box;

            p.setPen(QPen(sel ? kSelectColor : color, sel ? thick : thin, Qt::DashLine));

            p.setBrush(Qt::NoBrush);

            p.drawRect(box);

        }

        // 标签分两轮：先画可省的目标，选中项最后画（永远在最上层）

        QVector<int> order;

        for (int i = 0; i < objects.size(); ++i)

            if (i != selectedObj) order.append(i);

        if (selectedObj >= 0 && selectedObj < objects.size()) order.append(selectedObj);

        QVector<QRectF> used;

        for (int i : order) {

            const SeedObject &o = objects[i];

            const bool sel = (i == selectedObj);

            const QColor color = statusColor(o.status);

            QString text;

            if (sel) {

                // 实时链路没有 6 项指标，areaPx 恒为 0——别把 0 当成测量值写上去

                text = displayTag(o) + (o.hasMetrics

                    ? QString("  A=%1px²").arg(o.areaPx, 0, 'f', 0)

                    : QString("  c=%1").arg(o.confidence, 0, 'f', 2));

            } else if (!dense) {

                text = displayTag(o);

            }

            if (text.isEmpty()) continue;

            const QRect tr = fm.boundingRect(text).adjusted(-4, -2, 4, 2);

            QRectF bg(scaled[i].x(), scaled[i].y() - tr.height() - 2,

                      tr.width(), tr.height());

            if (bg.top() < 0) bg.moveTop(scaled[i].y() + 2);

            if (bg.x() < 0) bg.moveLeft(1);

            if (bg.right() > w) bg.moveRight(w - 1);

            // 自动避让：与已画标签相交的就不画了（否则字会糊成一片）

            bool clash = false;

            for (const QRectF &u : used)

                if (u.intersects(bg)) { clash = true; break; }

            if (clash && !sel) continue;

            used.append(bg);

            const QColor bgc = sel ? kSelectColor : color;

            p.fillRect(bg, QColor(bgc.red(), bgc.green(), bgc.blue(), sel ? 220 : 150));

            p.setPen(Qt::white);

            p.drawText(bg, Qt::AlignCenter, text);

        }

        // live 链路只出 YOLO 检测框，这里说清楚为什么没有 6 项指标

        const bool anyMetrics = std::any_of(objects.begin(), objects.end(),

            [](const SeedObject &o) { return o.hasMetrics; });

        if (!anyMetrics) {

            const QString hint = "实时链路只出检测框；6 项表型需载入检测结果文件";

            QFont hf("Microsoft YaHei UI");

            hf.setPixelSize(qBound(12, w / 28, 110));

            p.setFont(hf);

            const QFontMetrics hfm(hf);

            const int barW = qMin(img.width() - 16,

                                  hfm.horizontalAdvance(hint) + 16);

            const QRectF bar(8, img.height() - hfm.height() - 14, barW,

                             hfm.height() + 8);

            p.setPen(Qt::NoPen);

            p.setBrush(QColor(21, 62, 43, 190));

            p.drawRoundedRect(bar, 5, 5);

            p.setPen(Qt::white);

            p.drawText(bar, Qt::AlignCenter, hint);

        }

        // 选中粒的 6 项表型直接贴在图上（截图即可当作证据）

        if (selectedObj >= 0 && selectedObj < objects.size()) {

            const SeedObject &o = objects[selectedObj];

            QStringList lines{displayTag(o)};

            if (o.hasMetrics) {

                lines << QString("Area        %1 px²").arg(o.areaPx, 0, 'f', 1)

                      << QString("Length      %1 px").arg(o.lengthPx, 0, 'f', 2)

                      << QString("Width       %1 px").arg(o.widthPx, 0, 'f', 2)

                      << QString("Perimeter   %1 px").arg(o.perimeterPx, 0, 'f', 2)

                      << QString("AspectRatio %1 (-)").arg(o.aspectRatio, 0, 'f', 3)

                      << QString("Circularity %1 (-)").arg(o.circularity, 0, 'f', 4)

                      << QString("Status      %1").arg(o.status.isEmpty() ? "--" : o.status);

            } else {

                lines << QString("Conf        %1").arg(o.confidence, 0, 'f', 2)

                      << "实时链路只有检测框"

                      << "6 项表型需载入检测结果文件";

            }

            QFont pf("Consolas");

            pf.setPixelSize(qBound(12, w / 28, 110));

            p.setFont(pf);

            const QFontMetrics pfm(pf);

            int textW = 0;

            for (const QString &l : lines)

                textW = qMax(textW, pfm.horizontalAdvance(l));

            const int lh = pfm.height();

            const QRectF panel(8, 8, textW + 16, lh * lines.size() + 10);

            p.setPen(Qt::NoPen);

            p.setBrush(QColor(21, 62, 43, 205));

            p.drawRoundedRect(panel, 6, 6);

            p.setPen(Qt::white);

            for (int k = 0; k < lines.size(); ++k)

                p.drawText(QPointF(panel.x() + 8, panel.y() + 6 + lh * (k + 1) - 4),

                           lines[k]);

        }

        return img;

    };

    // 空状态画面：检测关掉或没找到目标时，不再把原图铺满再压一行小字

    // （那样看着像“有结果”，评委要凑近才看得见说明）。这里直接给一张

    // 明确的占位图：为什么是空的 + 怎么让它有内容。

    auto emptyView = [](const QString &title, const QString &subtitle) {

        const int W = 960, H = 720;

        QImage img(W, H, QImage::Format_RGB32);

        img.fill(QColor(243, 247, 238));

        QPainter p(&img);

        p.setRenderHint(QPainter::Antialiasing, true);

        const QPointF c(W / 2.0, H * 0.38);

        const double r = 58;

        p.setPen(QPen(QColor(178, 198, 174), 6));

        p.setBrush(QColor(232, 240, 226));

        p.drawEllipse(c, r, r);

        p.setPen(QPen(QColor(146, 170, 140), 7, Qt::SolidLine, Qt::RoundCap));

        p.drawLine(QPointF(c.x() - r * 0.5, c.y() - r * 0.5),

                   QPointF(c.x() + r * 0.5, c.y() + r * 0.5));

        // 占位图会按视图大小整体缩小（四视图里约缩到 1/3），字号要按缩小后可读来定

        QFont titleFont("Microsoft YaHei UI");

        titleFont.setPixelSize(44);

        titleFont.setBold(true);

        p.setFont(titleFont);

        p.setPen(QColor(54, 78, 58));

        p.drawText(QRectF(0, c.y() + r + 28, W, 60),

                   Qt::AlignHCenter | Qt::AlignTop, title);

        QFont subFont("Microsoft YaHei UI");

        subFont.setPixelSize(28);

        p.setFont(subFont);

        p.setPen(QColor(122, 140, 120));

        p.drawText(QRectF(80, c.y() + r + 98, W - 160, 110),

                   Qt::AlignHCenter | Qt::AlignTop | Qt::TextWordWrap, subtitle);

        return img;

    };

    redraw = [&]() {

        const FrameData *fd = current();

        QImage comp[4];

        if (fd) {

            comp[0] = fd->original;

            comp[1] = fd->fpga;

            // 没有目标时要说清原因：是用户把检测关了，还是检测开了但没找到。

            // 关检测时整张图铺“目标检测已关闭”会让人以为图就是结果，这里改成

            // 空状态画面，和真正的结果一眼就能分开。

            const bool detOn = detect->isChecked();

            if (!comp[0].isNull()) {

                if (!detOn)

                    comp[2] = emptyView(

                        "目标检测已关闭",

                        "在左侧「检测」中勾选「启用目标检测」，然后重新运行");

                else if (objects.isEmpty())

                    comp[2] = emptyView(

                        "未检测到目标",

                        "可尝试降低置信度阈值，或更换图片后重新运行");

                else

                    comp[2] = drawBoxes(comp[0], false);

            }

            if (!comp[1].isNull()) {

                if (!detOn)

                    comp[3] = emptyView(

                        "无单粒表型结果",

                        "单粒表型依赖目标检测，请先启用检测并重新运行");

                else if (objects.isEmpty())

                    comp[3] = emptyView("无单粒表型结果", "本次运行没有检测到目标");

                else

                    comp[3] = drawPheno(comp[1]);

            }

        }

        for (int i = 0; i < 4; ++i) {

            ClickableLabel *v = viewList[i];

            if (comp[i].isNull()) {

                v->setPixmap(QPixmap());

                v->setText(i == 3 ? "单粒表型：等待 FPGA 结果图" : "等待结果");

                viewStates[i] = ViewState();

                viewFitBtns[i]->setEnabled(false);

                continue;

            }

            const QRect cr = v->contentsRect();

            if (cr.width() < 8 || cr.height() < 8) continue;

            viewStates[i].composite = comp[i];

            // 注意：这里**不能**重置 userZoom/center。redraw 每 400ms 跑一次，

            // 一重置就等于「放大后 0.4 秒弹回适应态」。缩放归位交给换帧/重新载入

            // （见 switchFrame / loadJson 里的 resetZoom 调用）

            presentImage(v, &viewStates[i]);

            viewFitBtns[i]->setEnabled(viewStates[i].userZoom > 1.001);

        }

    };

    // ===================== segmentation Comparison 页逻辑 =====================

    // 契约 docs/segmentation_result_format.md。数字全部来自产出方，Qt 只读不重算。

    std::function<QString(const QString &)> loadComparison;

    std::function<void()> cmpFillList;

    std::function<void()> cmpRebuild;

    std::function<void(int)> cmpSelectObject;

    std::function<QImage(bool)> cmpDraw;

    // ---- 解析：单实例 ----

    auto parseCmpObject = [](const QJsonObject &o, int idx) -> CmpObject {

        CmpObject c;

        c.id = o.value("id").toInt(idx + 1);

        c.seedId = o.value("seed_id").toString();

        const QJsonArray bb = o.value("bbox").toArray();

        if (bb.size() == 4) {

            const double x1 = bb.at(0).toDouble(), y1 = bb.at(1).toDouble();

            const double x2 = bb.at(2).toDouble(), y2 = bb.at(3).toDouble();

            c.bbox = QRectF(QPointF(x1, y1), QPointF(x2, y2)).normalized();

        }

        const QJsonArray ct = o.value("contour").toArray();

        for (const QJsonValue &v : ct) {

            const QJsonArray pt = v.toArray();

            if (pt.size() == 2)

                c.contour << QPointF(pt.at(0).toDouble(), pt.at(1).toDouble());

        }

        c.hasConf = o.value("confidence").isDouble();

        c.confidence = o.value("confidence").toDouble();

        c.status = o.value("status").toString();

        if (o.value("parent_yolo_id").isDouble())

            c.parentYoloId = o.value("parent_yolo_id").toInt();

        if (o.value("area_px").isDouble()) c.areaPx = o.value("area_px").toDouble();

        // 标签锚点用轮廓质心：粘连团里几个实例的 bbox 中心会挤在一起

        if (!c.contour.isEmpty()) {

            double sx = 0, sy = 0;

            for (const QPointF &p : c.contour) { sx += p.x(); sy += p.y(); }

            c.labelPos = QPointF(sx / c.contour.size(), sy / c.contour.size());

        } else {

            c.labelPos = c.bbox.center();

        }

        return c;

    };

    // ---- 解析：单路 ----

    auto parseCmpMode = [&](const QJsonObject &m, bool *countFallback) -> CmpMode {

        CmpMode cm;

        if (m.isEmpty()) return cm;

        cm.present = true;

        const QJsonArray objs = m.value("objects").toArray();

        for (int i = 0; i < objs.size(); ++i)

            cm.objects.append(parseCmpObject(objs.at(i).toObject(), i));

        if (m.value("count").isDouble()) {

            cm.count = m.value("count").toInt();

        } else if (!objs.isEmpty()) {

            // 契约要求 count 必填。产出方漏了才走这里，并记一笔到日志 —— 不装作没事

            cm.count = objs.size();

            if (countFallback) *countFallback = true;

        }

        if (m.value("abs_error").isDouble()) cm.absError = m.value("abs_error").toInt();

        return cm;

    };

    loadComparison = [&](const QString &path) -> QString {

        QFile f(path);

        if (!f.open(QIODevice::ReadOnly))

            return "打不开文件：" + path;

        QJsonParseError pe{};

        const QJsonDocument doc = QJsonDocument::fromJson(f.readAll(), &pe);

        if (pe.error != QJsonParseError::NoError)

            return QString("JSON 解析失败（偏移 %1：%2）").arg(pe.offset).arg(pe.errorString());

        const QJsonObject rootObj = doc.object();

        const QJsonArray arr = rootObj.value("images").toArray();

        if (arr.isEmpty())

            return "JSON 里没有 images[]（对比结果文件要求顶层 images 数组）";

        cmpSchema = rootObj.value("schema").toString();

        cmpSource = rootObj.value("source").toString();

        cmpSourceNote = rootObj.value("source_note").toString();

        const QJsonObject ml = rootObj.value("mode_labels").toObject();

        if (ml.contains("yolo")) cmpModeLabel[0] = ml.value("yolo").toString();

        if (ml.contains("fixed")) cmpModeLabel[1] = ml.value("fixed").toString();

        if (ml.contains("adaptive")) cmpModeLabel[2] = ml.value("adaptive").toString();

        const QJsonObject chain = rootObj.value("chain_timings_ms").toObject();

        cmpChainFpga = chain.value("fpga_dma").isDouble()

            ? chain.value("fpga_dma").toDouble() : -1.0;

        cmpChainTotal = chain.value("total").isDouble()

            ? chain.value("total").toDouble() : -1.0;

        const QJsonObject det = rootObj.value("detector").toObject();

        cmpModeNote.clear();

        if (!det.isEmpty()) {

            // conf / iou 只有真给了才显示：B 的 NMS IoU 按品种不同，

            // 顶层写不下就不写，不能把缺省 0 显示成 "iou 0"

            QStringList detBits;

            if (det.value("conf").isDouble())

                detBits << QString("conf %1").arg(det.value("conf").toDouble());

            if (det.value("iou").isDouble())

                detBits << QString("iou %1").arg(det.value("iou").toDouble());

            cmpModeNote = det.value("name").toString();

            if (!detBits.isEmpty())

                cmpModeNote += QString("（%1）").arg(detBits.join(" / "));

        }

        QDir cmpDir(QFileInfo(path).absolutePath());

        cmpImages.clear();

        int countFallbacks = 0;

        QStringList missingImgs;

        for (const QJsonValue &v : arr) {

            const QJsonObject o = v.toObject();

            CmpImage ci;

            ci.imageId = o.value("image_id").toString();

            ci.file = o.value("file").toString();

            ci.species = o.value("species").toString();

            ci.contactType = o.value("contact_type").toString();

            ci.split = o.value("split").toString();

            ci.note = o.value("notes").toString();

            ci.failNote = o.value("fail_note").toString();

            ci.gtSource = o.value("gt_source").toString();

            ci.isFail = (o.value("status").toString() == "fail");

            if (o.value("gt_count").isDouble()) ci.gtCount = o.value("gt_count").toInt();

            const QJsonObject t = o.value("timings_ms").toObject();

            auto tm = [&](const char *k) {

                return t.value(k).isDouble() ? t.value(k).toDouble() : -1.0;

            };

            ci.tYolo = tm("yolo");

            ci.tFixed = tm("fixed");

            ci.tAdaptive = tm("adaptive");

            ci.tTotal = tm("total");

            ci.tTotalMode[0] = tm("total_yolo");

            ci.tTotalMode[1] = tm("total_fixed");

            ci.tTotalMode[2] = tm("total_adaptive");

            const QJsonObject modes = o.value("modes").toObject();

            bool fb = false;

            ci.modes[0] = parseCmpMode(modes.value("yolo").toObject(), &fb);

            ci.modes[1] = parseCmpMode(modes.value("fixed").toObject(), &fb);

            ci.modes[2] = parseCmpMode(modes.value("adaptive").toObject(), &fb);

            if (fb) ++countFallbacks;

            // 图像：先按仓库根解析相对路径，再试原样路径、JSON 同目录

            QString ip = findRepoFile(ci.file);

            if (ip.isEmpty() && QFileInfo::exists(ci.file)) ip = ci.file;

            if (ip.isEmpty()) ip = cmpDir.absoluteFilePath(ci.file);

            if (QFileInfo::exists(ip)) ci.original = QImage(ip);

            else missingImgs << ci.imageId;

            cmpImages.append(ci);

        }

        cmpLoadedPath = path;

        if (countFallbacks > 0)

            append(QString("提示：%1 路结果缺 count 字段，已按实例轮廓个数兜底。"

                           "「数量」应取产出方写的 count，不是数轮廓 —— 请让产出方补上")

                       .arg(countFallbacks));

        if (!missingImgs.isEmpty())

            append(QString("有 %1 张图找不到文件，已按空图处理：%2")

                       .arg(missingImgs.size()).arg(missingImgs.join(", ")));

        return QString();

    };

    // ---- 绘图：左 = YOLO bbox，右 = 实例轮廓填充 ----

    cmpDraw = [&](bool left) -> QImage {

        if (cmpIndex < 0 || cmpIndex >= cmpImages.size()) return QImage();

        const CmpImage &ci = cmpImages[cmpIndex];

        if (ci.original.isNull()) return QImage();

        QImage img = ci.original.convertToFormat(QImage::Format_RGB32);

        const int w = img.width();

        const int fontPx = qBound(14, w / 40, 30);

        const int thin = qMax(2, w / 400);

        const int thick = qMax(3, w / 200);

        QPainter p(&img);

        p.setRenderHint(QPainter::Antialiasing, true);

        QFont font("Arial");

        font.setPixelSize(fontPx);

        font.setBold(true);

        p.setFont(font);

        QFontMetrics fm(font);

        const CmpMode &cm = ci.modes[left ? 0 : cmpMode];

        if (cm.objects.isEmpty()) {

            p.setPen(QColor(120, 140, 120));

            p.drawText(img.rect(), Qt::AlignCenter,

                       left ? "这一路没有实例" : "未提供实例轮廓");

            return img;

        }

        // 右侧压暗一点底色，彩色轮廓才跳得出来

        if (!left) p.fillRect(img.rect(), QColor(255, 255, 255, 90));

        for (int i = 0; i < cm.objects.size(); ++i) {

            const CmpObject &o = cm.objects[i];

            const bool sel = (i == cmpSelected);

            const QColor c = instanceColor(i);

            if (left) {

                p.setPen(QPen(sel ? kSelectColor : QColor(198, 40, 40), sel ? thick : thin));

                p.setBrush(Qt::NoBrush);

                p.drawRect(o.bbox);

            } else if (o.contour.size() >= 3) {

                p.setPen(QPen(sel ? kSelectColor : c, sel ? thick + 1 : thick));

                p.setBrush(QColor(c.red(), c.green(), c.blue(), sel ? 110 : 70));

                p.drawPolygon(o.contour);

            } else {

                p.setPen(QPen(sel ? kSelectColor : QColor(120, 120, 120), thin, Qt::DashLine));

                p.setBrush(Qt::NoBrush);

                p.drawRect(o.bbox);

            }

        }

        // 标签：选中项最后画、必画；其余与已画标签相交就跳过（免得糊成一片）

        QVector<int> order;

        for (int i = 0; i < cm.objects.size(); ++i)

            if (i != cmpSelected) order.append(i);

        if (cmpSelected >= 0 && cmpSelected < cm.objects.size()) order.append(cmpSelected);

        const bool dense = cm.objects.size() > 14;

        QVector<QRectF> used;

        for (int i : order) {

            const CmpObject &o = cm.objects[i];

            const bool sel = (i == cmpSelected);

            if (dense && !sel) continue;

            const QColor c = instanceColor(i);

            QString text = o.tag();

            if (sel && o.hasConf) text += QString("  c=%1").arg(o.confidence, 0, 'f', 2);

            if (!sel && o.status == "UNRESOLVED") text += "  !";

            const QRect tr = fm.boundingRect(text).adjusted(-4, -2, 4, 2);

            QRectF bg;

            if (left) {

                // 左视图是字在框外上方，和 phenotyping 的 Detection 视图一个观感

                bg = QRectF(o.bbox.left(), o.bbox.top() - tr.height() - 2,

                            tr.width(), tr.height());

                if (bg.top() < 0) bg.moveTop(o.bbox.top() + 2);

            } else {

                // 右视图是字落在轮廓质心上（粘连团里框中心会挤成一点）

                bg = QRectF(o.labelPos.x() - tr.width() / 2.0,

                            o.labelPos.y() - tr.height() / 2.0,

                            tr.width(), tr.height());

            }

            if (bg.left() < 0) bg.moveLeft(1);

            if (bg.right() > w) bg.moveRight(w - 1);

            if (bg.bottom() > img.height()) bg.moveBottom(img.height() - 1);

            bool clash = false;

            for (const QRectF &u : used)

                if (u.intersects(bg)) { clash = true; break; }

            if (clash && !sel) continue;

            used.append(bg);

            const QColor bgc = sel ? kSelectColor : c;

            p.fillRect(bg, QColor(bgc.red(), bgc.green(), bgc.blue(), sel ? 225 : 185));

            p.setPen(Qt::white);

            p.drawText(bg, Qt::AlignCenter, text);

        }

        if (!left) {

            // 图例：颜色 ↔ 状态，说明"哪种颜色是什么意思"其实靠的是状态色，别让人猜

            int unresolved = 0;

            for (const CmpObject &o : cm.objects)

                if (o.status == "UNRESOLVED") ++unresolved;

            if (unresolved > 0) {

                const QString leg = QString("UNRESOLVED %1 个：轮廓可信度不足（灰标 !），仍算进计数")

                                         .arg(unresolved);

                QFont lf("Microsoft YaHei UI");

                lf.setPixelSize(qBound(12, w / 48, 20));

                p.setFont(lf);

                const QFontMetrics lfm(lf);

                const QRect lr = lfm.boundingRect(leg).adjusted(-6, -4, 6, 4);

                const QRectF box(8, img.height() - lr.height() - 8, lr.width(), lr.height());

                p.setPen(Qt::NoPen);

                p.setBrush(QColor(60, 60, 60, 200));

                p.drawRoundedRect(box, 5, 5);

                p.setPen(Qt::white);

                p.drawText(box, Qt::AlignCenter, leg);

            }

        }

        return img;

    };

    // 把绘图结果等比缩放进 QLabel，并记住缩放/偏移供点击反算（与表型页同一套）

    auto cmpPresent = [&](ClickableLabel *v, ViewState *vs, bool left) {

        const QImage img = cmpDraw(left);

        if (img.isNull()) {

            v->setPixmap(QPixmap());

            v->setText("等待对比数据");

            *vs = ViewState();

            return;

        }

        const QRect cr = v->contentsRect();

        if (cr.width() < 8 || cr.height() < 8) return;

        vs->composite = img;

        // 和 redraw 一样：不碰 userZoom/center，否则 400ms 刷新会把放大弹回去

        presentImage(v, vs);

    };

    // ---- 列表：当前模式实例 ----

    auto cmpFillTable = [&]() {

        selectionSync = true;

        cmpTable->setRowCount(0);

        if (cmpIndex < 0 || cmpIndex >= cmpImages.size()) {

            selectionSync = false;

            return;

        }

        const CmpMode &cm = cmpImages[cmpIndex].modes[cmpMode];

        cmpTable->setRowCount(cm.objects.size());

        for (int i = 0; i < cm.objects.size(); ++i) {

            const CmpObject &o = cm.objects[i];

            const QString warn = (o.status.isEmpty() || o.status == "OK") ? QString()

                                                                          : QString("⚠ ");

            auto *c0 = new QTableWidgetItem(warn + o.tag());

            auto *c1 = new QTableWidgetItem(o.status.isEmpty() ? "--" : o.status);

            c1->setToolTip(c1->text());   // 列窄时状态被省略，悬停看全称

            QString src = QString("框 #%1").arg(o.parentYoloId);

            if (o.parentYoloId < 0)

                src = o.contour.isEmpty() ? "YOLO 原框" : "无对应框";

            auto *c2 = new QTableWidgetItem(src);

            c2->setToolTip(c2->text());

            QString area = "--";

            if (o.areaPx >= 0) area = QString::number(o.areaPx, 'f', 0);

            else if (o.hasConf) area = QString("c=%1").arg(o.confidence, 0, 'f', 2);

            auto *c3 = new QTableWidgetItem(area);

            const QColor sc = statusColor(o.status);

            if (o.status == "UNRESOLVED") {

                for (QTableWidgetItem *it : {c0, c1, c2, c3}) it->setForeground(sc);

            } else if (o.status != "OK" && !o.status.isEmpty()) {

                for (QTableWidgetItem *it : {c0, c1}) it->setForeground(sc);

            }

            cmpTable->setItem(i, 0, c0);

            cmpTable->setItem(i, 1, c1);

            cmpTable->setItem(i, 2, c2);

            cmpTable->setItem(i, 3, c3);

        }

        if (cmpSelected >= 0 && cmpSelected < cm.objects.size())

            cmpTable->selectRow(cmpSelected);

        else

            cmpTable->clearSelection();

        selectionSync = false;

    };

    // 记住上次呈现时的控件尺寸：只有尺寸变了才需要重画（否则 400ms 刷新会白烧 CPU）

    QSize cmpLastSize[2];

    auto cmpPresentAll = [&]() {

        cmpPresent(cmpLeftView, &cmpLeftState, true);

        cmpPresent(cmpRightView, &cmpRightState, false);

        cmpLastSize[0] = cmpLeftView->contentsRect().size();

        cmpLastSize[1] = cmpRightView->contentsRect().size();

        cmpLeftFit->setEnabled(cmpLeftState.userZoom > 1.001);

        cmpRightFit->setEnabled(cmpRightState.userZoom > 1.001);

    };

    // ---- 图像放大窗 ----

    openInspect = [&](bool cmpSide_, int startIdx) {

        QVector<ImageViewerDialog::Item> items;

        // 实时结果的大窗要能跟着新帧走，所以四格都建出来（暂时没图的先空着），

        // 由 setLiveProvider 的回调后续填。离线载入/对比页仍只放有图的格。

        const bool liveFollow = !cmpSide_ && showLive;

        if (liveFollow) {

            for (int i = 0; i < viewList.size(); ++i)

                items << ImageViewerDialog::Item{viewTitles[i],

                                                 viewStates[i].composite};

        } else if (cmpSide_) {

            if (!cmpLeftState.composite.isNull())

                items << ImageViewerDialog::Item{cmpLeftCap->text(),

                                                 cmpLeftState.composite};

            if (!cmpRightState.composite.isNull())

                items << ImageViewerDialog::Item{cmpRightCap->text(),

                                                 cmpRightState.composite};

        } else {

            for (int i = 0; i < viewList.size(); ++i)

                if (!viewStates[i].composite.isNull())

                    items << ImageViewerDialog::Item{viewTitles[i],

                                                     viewStates[i].composite};

        }

        if (items.isEmpty()) {

            append("放大窗：当前还没有可显示的图像");

            return;

        }

        // 只留一个窗：开了第二个却不知道旧的在后面，会以为界面卡了

        if (inspectDlg) {

            inspectDlg->close();

            inspectDlg = nullptr;

        }

        inspectDlg = new ImageViewerDialog(&win, items, 2, startIdx);

        if (liveFollow) {

            inspectDlg->setLiveProvider([&, titles = viewTitles]() {

                QVector<ImageViewerDialog::Item> latest;

                for (int i = 0; i < viewList.size(); ++i)

                    latest << ImageViewerDialog::Item{titles[i],

                                                      viewStates[i].composite};

                return latest;

            });

        }

        inspectDlg->show();

        append(QString("放大窗已打开：%1 格（滚轮缩放 / 按住拖动平移 / Esc 关闭%2）")

                   .arg(items.size())

                   .arg(liveFollow ? "；默认跟随实时，可取消勾选定格" : ""));

    };

    // 换帧 / 重新载入时把缩放归位。留着上一张图的放大位置没有意义 ——

    // 看到的是同一块像素坐标、内容却换了的区域，像是图被裁掉了一半

    auto resetZoom = [&]() {

        for (int i = 0; i < 4; ++i) {

            viewStates[i].userZoom = 1.0;

            viewStates[i].center = QPointF(0.5, 0.5);

            viewFitBtns[i]->setEnabled(false);

        }

        for (ViewState *vs : {&cmpLeftState, &cmpRightState}) {

            vs->userZoom = 1.0;

            vs->center = QPointF(0.5, 0.5);

        }

        cmpLeftFit->setEnabled(false);

        cmpRightFit->setEnabled(false);

    };

    auto setTimeLabel = [&](QLabel *l, double ms) {

        const bool na = (ms < 0);

        l->setText(na ? "N/A" : QString::number(ms, 'f', 1) + " ms");

        const QString want = na ? "cmpNA" : "detailValue";

        if (l->objectName() != want) {

            l->setObjectName(want);

            l->style()->unpolish(l);

            l->style()->polish(l);

        }

    };

    cmpRebuild = [&]() {

        const bool has = (cmpIndex >= 0 && cmpIndex < cmpImages.size());

        // 模式按钮文案跟着 JSON 的 mode_labels 走

        cmpBtnFixed->setText(cmpModeLabel[1]);

        cmpBtnAdaptive->setText(cmpModeLabel[2]);

        // 文案写短：窄窗下标题一折行就吃掉视图高度；当前分离模式在右栏已经写明

        cmpLeftCap->setText("① 检测框");

        cmpLeftCap->setToolTip("原图 + YOLO 检测框");

        // 当前模式在右栏分段按钮上已经高亮，标题不再重复，窄栏里才不会折行

        cmpRightCap->setText("② 分离轮廓");

        cmpRightCap->setToolTip(QString("改进分离实例轮廓 · 当前模式：%1")

                                    .arg(cmpModeLabel[cmpMode]));

        mockBanner->setVisible(cmpSource == "mock");

        if (cmpSource == "mock") {

            mockNote->setText(cmpSourceNote.isEmpty()

                ? QStringLiteral("本页所有数字都是模拟数据，不是实验结果")

                : cmpSourceNote);

        }

        if (!has) {

            for (int i = 0; i < 3; ++i) cmpBigVal[i]->setText("--");

            for (int i = 0; i < 4; ++i) cmpInfoVal[i]->setText("--");

            for (int i = 0; i < 4; ++i) setTimeLabel(cmpTimeVal[i], -1);

            cmpGtNote->clear();

            cmpFailNote->setVisible(false);

            cmpTable->setRowCount(0);

            cmpPresentAll();

            cmpListStat->setText(cmpImages.isEmpty() ? "还没载入对比 JSON" : "--");

            return;

        }

        const CmpImage &ci = cmpImages[cmpIndex];

        const CmpMode &yolo = ci.modes[0];

        const CmpMode &cur = ci.modes[cmpMode];

        cmpBigVal[0]->setText(intText(yolo.count));

        cmpBigVal[1]->setText(intText(cur.count));

        cmpBigVal[2]->setText(intText(cur.absError));

        for (int i = 0; i < 3; ++i)

            cmpBigVal[i]->setObjectName(cmpBigVal[i]->text() == "N/A" ? "cmpBigNA" : "cmpBig");

        for (int i = 0; i < 3; ++i) {

            cmpBigVal[i]->style()->unpolish(cmpBigVal[i]);

            cmpBigVal[i]->style()->polish(cmpBigVal[i]);

        }

        cmpInfoVal[0]->setText(ci.species.isEmpty() ? "未标注" : ci.species);

        if (ci.gtCount >= 0) {

            cmpInfoVal[1]->setText(QString::number(ci.gtCount));

            cmpInfoVal[1]->setObjectName("detailValue");

        } else {

            cmpInfoVal[1]->setText("N/A（无人工标注）");

            cmpInfoVal[1]->setObjectName("cmpNA");

        }

        cmpInfoVal[1]->style()->unpolish(cmpInfoVal[1]);

        cmpInfoVal[1]->style()->polish(cmpInfoVal[1]);

        // 分离段只属于 fixed/adaptive；原 YOLO 路没有分离，显示 N/A 而不是别的模式

        const double sepMs = cmpMode == 0 ? -1.0

                           : (cmpMode == 1 ? ci.tFixed : ci.tAdaptive);

        // 整链路优先用当前模式的 total_<mode>（适配器可选扩展），否则回落单值

        const double totalMs = (cmpMode >= 0 && cmpMode < 3

                                && ci.tTotalMode[cmpMode] >= 0)

            ? ci.tTotalMode[cmpMode]

            : (ci.tTotal >= 0 ? ci.tTotal : cmpChainTotal);

        setTimeLabel(cmpInfoVal[2], sepMs);

        cmpInfoVal[3]->setText(ci.imageId);

        QStringList gtBits;

        if (ci.gtCount >= 0) {

            gtBits << QString("GT 由人工点数：%1")

                          .arg(ci.gtSource.isEmpty() ? "来源未注明" : ci.gtSource);

            if (cur.absError >= 0)

                gtBits << QString("绝对误差 = |%1 - %2| = %3")

                              .arg(cur.count).arg(ci.gtCount).arg(cur.absError);

        } else {

            gtBits << "这张图没有人工标注 → GT 与绝对误差都显示 N/A（不猜）";

        }

        cmpGtNote->setText(gtBits.join("；"));

        cmpFailNote->setVisible(!ci.failNote.isEmpty());

        cmpFailNote->setText("⚠ " + ci.failNote);

        cmpGtNote->setVisible(!gtBits.isEmpty());

        setTimeLabel(cmpTimeVal[0], ci.tYolo);

        setTimeLabel(cmpTimeVal[1], sepMs);

        setTimeLabel(cmpTimeVal[2], cmpChainFpga);

        setTimeLabel(cmpTimeVal[3], totalMs);

        // 换图 / 换分离模式都走这里重建，缩放也跟着复位 ——

        // 留着上一张的放大位置，看到的会是同一块像素坐标、内容却不同了的区域

        for (ViewState *vs : {&cmpLeftState, &cmpRightState}) {

            vs->userZoom = 1.0;

            vs->center = QPointF(0.5, 0.5);

        }

        cmpFillTable();

        cmpPresentAll();

    };

    cmpSelectObject = [&](int idx) {

        cmpSelected = (idx >= 0 && cmpIndex >= 0 && cmpIndex < cmpImages.size()

                       && idx < cmpImages[cmpIndex].modes[cmpMode].objects.size())

            ? idx : -1;

        if (cmpSelected >= 0 && cmpSelected < cmpTable->rowCount()) {

            selectionSync = true;

            cmpTable->selectRow(cmpSelected);

            cmpTable->scrollToItem(cmpTable->item(cmpSelected, 0));

            selectionSync = false;

        }

        cmpPresentAll();

    };

    // 点击：右图选实例；左图选 YOLO 框 → 连带选中当前模式里由它拆出的实例

    cmpRightView->onClicked = [&](QPoint pos) {

        if (cmpIndex < 0 || cmpIndex >= cmpImages.size()) return;

        const ViewState &vs = cmpRightState;

        if (vs.composite.isNull() || vs.scale <= 0) return;

        const QPointF ip = (QPointF(pos) - vs.offset) / vs.scale;

        const CmpMode &cm = cmpImages[cmpIndex].modes[cmpMode];

        int hit = -1;

        for (int i = 0; i < cm.objects.size(); ++i) {

            if (cm.objects[i].contour.size() >= 3

                && cm.objects[i].contour.containsPoint(ip, Qt::OddEvenFill)) {

                hit = i;

                break;

            }

        }

        if (hit < 0) {                       // 轮廓点不中的，退一步按 bbox

            for (int i = 0; i < cm.objects.size(); ++i) {

                if (cm.objects[i].bbox.contains(ip)) { hit = i; break; }

            }

        }

        cmpSelectObject(hit);

        append(hit < 0 ? QString("对比：点击处没有实例（图像坐标 %1,%2）")

                             .arg(ip.x(), 0, 'f', 0).arg(ip.y(), 0, 'f', 0)

                       : QString("对比：选中 %1（%2）")

                             .arg(cm.objects[hit].tag()).arg(cm.objects[hit].status));

    };

    cmpLeftView->onClicked = [&](QPoint pos) {

        if (cmpIndex < 0 || cmpIndex >= cmpImages.size()) return;

        const ViewState &vs = cmpLeftState;

        if (vs.composite.isNull() || vs.scale <= 0) return;

        const QPointF ip = (QPointF(pos) - vs.offset) / vs.scale;

        const CmpImage &ci = cmpImages[cmpIndex];

        int yoloHit = -1;

        for (int i = 0; i < ci.modes[0].objects.size(); ++i) {

            if (ci.modes[0].objects[i].bbox.contains(ip)) { yoloHit = i; break; }

        }

        if (yoloHit < 0) { cmpSelectObject(-1); append("对比：点击处没有 YOLO 框"); return; }

        const int yid = ci.modes[0].objects[yoloHit].id;

        const CmpMode &cm = ci.modes[cmpMode];

        int idx = -1;

        for (int i = 0; i < cm.objects.size(); ++i) {

            const CmpObject &o = cm.objects[i];

            // parent_yolo_id 对得上，或者实例框与这个 YOLO 框明显重叠

            const bool byParent = (o.parentYoloId == yid);

            const bool byGeom = (o.bbox.intersected(ci.modes[0].objects[yoloHit].bbox).width()

                                 > 0.5 * o.bbox.width());

            if (byParent || byGeom) { idx = i; break; }

        }

        cmpSelectObject(idx);

        append(idx < 0 ? QString("对比：YOLO 框 #%1 在「%2」里没有对应实例")

                             .arg(yid).arg(cmpModeLabel[cmpMode])

                       : QString("对比：点 YOLO 框 #%1 → 选中 %2")

                             .arg(yid).arg(cm.objects[idx].tag()));

    };

    // ---- 图像下拉：当前对比图 + 失败筛选 ----

    // 下拉当前行没变时 QComboBox 不发信号，这里手动补一次重绘，

    // 否则载入后第一张 / --cmp-index 0 可能停在旧图上

    auto cmpSelectRow = [&](int row) {

        if (row < 0 || row >= cmpListMap.size()) return;

        const int target = cmpListMap[row];

        if (cmpCombo->currentIndex() != row) {

            cmpCombo->setCurrentIndex(row);      // 触发 currentIndexChanged

            return;

        }

        if (cmpIndex != target) {

            cmpIndex = target;

            cmpSelected = -1;

            cmpRebuild();

        }

    };

    cmpFillList = [&]() {

        const bool onlyFail = cmpOnlyFail->isChecked();

        cmpListMap.clear();

        const int keep = cmpIndex;

        cmpCombo->blockSignals(true);

        cmpCombo->clear();

        int fails = 0, total = 0;

        for (int i = 0; i < cmpImages.size(); ++i) {

            ++total;

            if (cmpImages[i].isFail) ++fails;

            if (onlyFail && !cmpImages[i].isFail) continue;

            const CmpImage &ci = cmpImages[i];

            QStringList bits;

            if (!ci.species.isEmpty()) bits << ci.species;

            bits << QString("Y%1").arg(intText(ci.modes[0].count))

                 << QString("F%1").arg(intText(ci.modes[1].count))

                 << QString("A%1").arg(intText(ci.modes[2].count));

            bits << (ci.gtCount >= 0 ? QString("GT%1").arg(ci.gtCount) : QString("GT--"));

            // 失败用 "! " 前缀 + 红字；每路数量与真值放进 tooltip

            const QString head = ci.isFail ? QString("! ") + ci.imageId : ci.imageId;

            const int row = cmpCombo->count();

            cmpCombo->addItem(head + " · " + bits.join(" / "), i);

            cmpCombo->setItemData(row, bits.join(" · "), Qt::ToolTipRole);

            if (ci.isFail)

                cmpCombo->setItemData(row, QColor(179, 65, 31), Qt::ForegroundRole);

            cmpListMap.append(i);

        }

        cmpCombo->blockSignals(false);

        const int row = cmpListMap.indexOf(keep);

        if (row >= 0) {

            cmpCombo->setCurrentIndex(row);

        } else if (!cmpListMap.isEmpty()) {

            cmpCombo->setCurrentIndex(0);        // 当前图被筛掉了就跳到第一张

        }

        cmpListStat->setText(QString("共 %1 · 失败 %2 · 显示 %3")

                                 .arg(total).arg(fails).arg(cmpCombo->count()));

        cmpListStat->setToolTip(cmpModeNote);

    };

    QObject::connect(cmpCombo, &QComboBox::currentIndexChanged, [&](int row) {

        if (row < 0 || row >= cmpListMap.size()) return;

        cmpIndex = cmpListMap[row];

        cmpSelected = -1;

        cmpRebuild();

        append(QString("对比：切到 %1").arg(cmpImages[cmpIndex].imageId));

    });

    QObject::connect(cmpOnlyFail, &QCheckBox::toggled, [&]() {

        cmpFillList();

        if (cmpIndex >= 0 && !cmpListMap.contains(cmpIndex) && !cmpListMap.isEmpty())

            cmpSelectRow(0);                     // 当前图被筛掉了就跳到第一张

        else

            cmpRebuild();

    });

    // 界面上载入对比结果：和 --cmp 走同一条 loadComparison，不重算任何数字

    // switchTab：工具栏手动载入才跳页；统一分析时留在当前页，用户自己切页签看结果

    auto loadCmpFromUi = [&](const QString &path, bool switchTab, bool quiet,

                             bool silent = false) {

        const QString err = loadComparison(path);

        if (!err.isEmpty()) {

            if (!quiet) QMessageBox::warning(&win, "载入失败", err);

            append("对比结果载入失败：" + err);

            return;

        }

        if (!silent)

            append(QString("对比结果载入：%1（%2 张图）").arg(path).arg(cmpImages.size()));

        if (switchTab) tabs->setCurrentIndex(1);

        cmpOnlyFail->setChecked(false);      // 新文件先看全部，别被上一次的筛选藏住

        cmpFillList();

        if (cmpCombo->count() > 0) cmpSelectRow(0);

    };

    QObject::connect(cmpLoad, &QPushButton::clicked, [&]() {

        const QString start = cmpLoadedPath.isEmpty()

            ? QDir::currentPath() : QFileInfo(cmpLoadedPath).absolutePath();

        const QString path = QFileDialog::getOpenFileName(

            &win, "载入对比结果", start, "JSON (*.json)");

        if (!path.isEmpty()) loadCmpFromUi(path, true, false);

    });

    QObject::connect(cmpLoadDemo, &QPushButton::clicked, [&]() {

        // 优先 B 的真实结果；没有才退回模拟数据（MOCK 横幅会自己出来）

        QString path = findRepoFile("data/evidence/demo/segmentation_examples/real_comparison.json");

        if (path.isEmpty())

            path = findRepoFile("data/evidence/demo/segmentation_examples/mock_comparison.json");

        if (path.isEmpty()) {

            append("找不到演示数据：请确认演示数据文件已随程序一同交付");

            return;

        }

        loadCmpFromUi(path, true, false);

    });

    // ---- 统一分析：一份输入 -> 单粒表型 + 粘连分离 两份结果 ----

    // 单张/批量不再先跑实时后端：直接调运行时脚本，一次把两份 JSON 都写出来，

    // 再分别载入两个页签。实时页的「分析当前帧」也走这里。

    auto startUnifiedAnalysis = [&](const QString &input, bool isDir,

                                    const QString &speciesKey, int gt,

                                    bool useBoard, bool switchTab,

                                    bool wantPheno, bool wantCmp,

                                    bool liveMode, const QString &tag) {

        if (genProcess.state() != QProcess::NotRunning) {

            // 实时自动分析：上一轮没结束就跳过这一拍，下一拍再取最新帧

            if (!liveMode) append(tag + "：上一轮分析还在跑，请稍候");

            return;

        }

        if (useBoard && !liveMode) {
            QMessageBox::information(&win, "分析路径说明",
                "单图和批量表型分析在本机执行。请将来源切换为本地；板卡实时预处理请使用实时模式。\n"
                "此入口不会把软件分析标记为 FPGA 处理。");
            return;
        }
        if (useBoard && liveMode)
            append("当前帧的检测、分离和表型在 CPU 执行；板卡状态见实时预处理链路。");

        const QString runtimeScript =

            findRepoFile("src/software/app/comparison_runtime.py");

        if (runtimeScript.isEmpty()) {

            if (liveMode) {

                liveAnalyze.stop();

                append("实时分析已停止：找不到现场分析脚本");

            } else {

                QMessageBox::warning(&win, "缺少分析脚本",

                                     "找不到现场分析脚本，请确认脚本已随程序一同交付");

            }

            return;

        }

        QDir().mkpath(runDir);

        // 注意：QDir::mkpath 只建目录，不会改变 QDir 自身的路径；

        // 必须显式把 analysis 拼进去，否则两份 JSON 会落回运行目录根下

        const QString analysisRoot =

            QDir(runDir).absoluteFilePath("analysis");

        QDir().mkpath(analysisRoot);

        const QString out = QDir(analysisRoot).absoluteFilePath("comparison.json");

        const QString pheno =

            QDir(analysisRoot).absoluteFilePath("phenotyping.json");

        QStringList args{QFileInfo(runtimeScript).absoluteFilePath(),

                         "--species", speciesKey};

        // 只勾一种时就不写另一种 JSON：少一次落盘，也不会让另一个页签

        // 误以为自己拿到了这次的新结果

        if (wantCmp) args << "--out" << out;

        if (wantPheno) args << "--pheno-out" << pheno;

        if (isDir) args << "--dir" << input;

        else args << "--image" << input;

        if (!isDir && gt >= 0) args << "--gt" << QString::number(gt);

        // Analysis uses the local BGR snapshot; board preprocessing runs separately.

        QDir repoDir(QFileInfo(runtimeScript).absolutePath());

        repoDir.cdUp();

        repoDir.cdUp();

        genProcess.setWorkingDirectory(repoDir.absolutePath());

        QProcessEnvironment env = QProcessEnvironment::systemEnvironment();

        env.insert("PYTHONUTF8", "1");

        env.insert("PYTHONIOENCODING", "utf-8");

        genProcess.setProcessEnvironment(env);

        genOutPath = wantCmp ? out : QString();

        genPhenoPath = wantPheno ? pheno : QString();

        genWantPheno = wantPheno;

        genWantCmp = wantCmp;

        genLive = liveMode;

        genSwitchTab = switchTab;

        genQuiet = liveMode || !switchTab;

        genTag = tag;

        // 实时分析在采集旁边跑，不能锁住界面，否则「停止」都按不了

        genBusyOwner = !liveMode;

        if (!liveMode) {

            if (setBusyFn) setBusyFn(true);

            cmpGenerate->setEnabled(false);

            cmpGenerate->setText(tag + "中…");

            QStringList outputs;

            if (wantPheno) outputs << "单粒表型";

            if (wantCmp) outputs << "粘连分离";

            append(QString("%1：%2（%3 / %4%5）")

                       .arg(tag, QFileInfo(input).fileName(),

                            species->currentText(),

                            useBoard ? "经板端" : "本机算法",

                            (!isDir && gt >= 0) ? QString(" / GT %1").arg(gt)

                                                : QString()));

            append("  本次产出：" + outputs.join(" + ")

                   + "（产出后留在当前页，切页签查看）");

        }

        genProcess.start(python->text().trimmed(), args);

    };

    // ---- 现场生成：图片 / 目录 / 最新实时帧 -> 算法 -> 契约 JSON ----

    // 与「载入」分开：载入是只读别人给的结果，生成是真的跑一遍算法。

    QObject::connect(cmpGenerate, &QPushButton::clicked, [&]() {

        if (genProcess.state() != QProcess::NotRunning) {

            append("生成对比：上一轮还在跑，请稍候");

            return;

        }

        const QString runtimeScript =

            findRepoFile("src/software/app/comparison_runtime.py");

        if (runtimeScript.isEmpty()) {

            QMessageBox::warning(&win, "缺少生成脚本",

                                 "找不到现场分析脚本，请确认脚本已随程序一同交付");

            return;

        }

        auto resolveInput = [&](const QString &p) {

            if (p.isEmpty() || QFileInfo::exists(p)) return p;

            const QString found = findRepoFile(p);

            return found.isEmpty() ? p : found;

        };

        auto currentInput = [&]() -> QString {

            if ((modeValue == "Image" || modeValue == "Batch")

                && !inputPath.isEmpty())

                return resolveInput(inputPath);

            if (modeValue == "Live") {

                const QString root = QDir(outDirEdit->text()).absolutePath();

                for (const char *name : {"cam_bgr_latest.png",

                                         "cam_latest.png"}) {

                    const QString p =

                        QDir(root).absoluteFilePath(QString("preview/") + name);

                    if (QFileInfo::exists(p)) return p;

                }

            }

            return QString();

        };

        const QString initialInput = currentInput();

        QDialog dlg(&win);

        dlg.setWindowTitle("生成对比结果");

        dlg.setMinimumWidth(560);

        auto *dlgLay = new QVBoxLayout(&dlg);

        auto *inputRow = new QHBoxLayout;

        inputRow->addWidget(new QLabel("输入"));

        auto *inputEdit = new QLineEdit(initialInput);

        inputEdit->setPlaceholderText("图片文件或目录；留空请先选择");

        inputRow->addWidget(inputEdit, 1);

        auto *pickImage = new QPushButton("选择图片…");

        auto *pickDir = new QPushButton("选择目录…");

        auto *useCurrent = new QPushButton("用当前输入");

        for (QPushButton *b : {pickImage, pickDir, useCurrent})

            b->setCursor(Qt::PointingHandCursor);

        inputRow->addWidget(pickImage);

        inputRow->addWidget(pickDir);

        inputRow->addWidget(useCurrent);

        dlgLay->addLayout(inputRow);

        auto *inputHint = new QLabel;

        inputHint->setObjectName("hint");

        inputHint->setWordWrap(true);

        inputHint->setText(

            initialInput.isEmpty()

                ? QString("当前模式：%1。还没有可用的实时帧 —— 先开始采集，"

                          "或点「选择图片…」。一份输入会同时产出表型与粘连两份结果。")

                      .arg(modeValue)

                : QString("当前模式：%1。图片会先归一到 640×480 再跑算法；"

                          "目录一次用一种品种。一份输入会同时产出表型与粘连两份结果。")

                      .arg(modeValue));

        dlgLay->addWidget(inputHint);

        auto *optRow = new QHBoxLayout;

        optRow->addWidget(new QLabel("品种"));

        auto *speciesBox = new QComboBox;

        speciesBox->addItem("黄豆", "soybean");

        speciesBox->addItem("绿豆", "mungbean");

        speciesBox->addItem("红豆", "redbean");

        speciesBox->addItem("其他", "other");

        // 当前对比图已经标了品种就跟着它走，省一次手选

        if (cmpIndex >= 0 && cmpIndex < cmpImages.size()) {

            const QString s = cmpImages[cmpIndex].species;

            const int sp = s.contains("绿") ? 1

                         : s.contains("红") ? 2

                         : s.contains("其") ? 3 : 0;

            speciesBox->setCurrentIndex(sp);

        }

        optRow->addWidget(speciesBox);

        optRow->addSpacing(12);

        optRow->addWidget(new QLabel("人工点数"));

        auto *gtSpin = new QSpinBox;

        gtSpin->setRange(-1, 9999);

        gtSpin->setValue(-1);

        gtSpin->setSpecialValueText("未标注");

        gtSpin->setToolTip("只对单张图生效；目录输入不设 GT，显示 N/A");

        optRow->addWidget(gtSpin);

        optRow->addStretch();

        dlgLay->addLayout(optRow);

        auto *phenoChk = new QCheckBox(

            "同时生成单粒表型结果（可在「单粒表型测量」页查看）");

        phenoChk->setChecked(true);

        phenoChk->setToolTip("勾上后同一份输入会多写一份表型 JSON，"

                             "两个页签分别显示对应结果");

        dlgLay->addWidget(phenoChk);

        auto *boardChk = new QCheckBox("本入口为本机软件分析；板卡预处理请使用实时模式");

        boardChk->setChecked(false);
        boardChk->setEnabled(false);

        auto *boardRow = new QHBoxLayout;

        boardRow->addWidget(boardChk);

        auto *boardHost = new QLineEdit(host->text());

        boardHost->setMaximumWidth(140);

        auto *boardPort = new QSpinBox;

        boardPort->setRange(1, 65535);

        boardPort->setValue(cli.value(optPort).toInt());

        boardPort->setMaximumWidth(90);

        boardRow->addWidget(new QLabel("地址"));

        boardRow->addWidget(boardHost);

        boardRow->addWidget(new QLabel("端口"));

        boardRow->addWidget(boardPort);

        boardRow->addStretch();

        dlgLay->addLayout(boardRow);

        auto *outRow = new QHBoxLayout;

        outRow->addWidget(new QLabel("输出"));

        const QString defaultOut = QDir(outDirEdit->text())

            .absoluteFilePath("comparison_runtime.json");

        auto *outEdit = new QLineEdit(defaultOut);

        outRow->addWidget(outEdit, 1);

        auto *outPick = new QPushButton("另存为…");

        outPick->setCursor(Qt::PointingHandCursor);

        outRow->addWidget(outPick);

        dlgLay->addLayout(outRow);

        auto *bb = new QDialogButtonBox;

        auto *okBtn = bb->addButton("开始生成", QDialogButtonBox::AcceptRole);

        bb->addButton("取消", QDialogButtonBox::RejectRole);

        okBtn->setCursor(Qt::PointingHandCursor);

        dlgLay->addWidget(bb);

        QObject::connect(pickImage, &QPushButton::clicked, [&]() {

            const QString p = QFileDialog::getOpenFileName(

                &dlg, "选择图片", QDir::homePath(),

                "图片 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)");

            if (!p.isEmpty()) inputEdit->setText(p);

        });

        QObject::connect(pickDir, &QPushButton::clicked, [&]() {

            const QString p = QFileDialog::getExistingDirectory(

                &dlg, "选择图片目录", QDir::homePath());

            if (!p.isEmpty()) inputEdit->setText(p);

        });

        QObject::connect(useCurrent, &QPushButton::clicked, [&]() {

            inputEdit->setText(currentInput());

        });

        QObject::connect(outPick, &QPushButton::clicked, [&]() {

            QString p = QFileDialog::getSaveFileName(

                &dlg, "保存对比结果", outEdit->text(), "JSON (*.json)");

            if (!p.isEmpty()) {

                if (!p.endsWith(".json", Qt::CaseInsensitive)) p += ".json";

                outEdit->setText(p);

            }

        });

        QObject::connect(boardChk, &QCheckBox::toggled, [&](bool on) {

            boardHost->setEnabled(on);

            boardPort->setEnabled(on);

        });

        boardHost->setEnabled(boardChk->isChecked());

        boardPort->setEnabled(boardChk->isChecked());

        QObject::connect(bb, &QDialogButtonBox::accepted, [&]() {

            const QString input = resolveInput(inputEdit->text().trimmed());

            if (input.isEmpty() || !QFileInfo::exists(input)) {

                QMessageBox::warning(&dlg, "输入不对",

                                     "请选择一个存在的图片或目录。");

                return;

            }

            if (QFileInfo(input).isDir() && gtSpin->value() >= 0) {

                QMessageBox::warning(&dlg, "GT 不适用",

                                     "目录输入不能套一个统一的人工点数；"

                                     "请留空（结果里 GT 显示 N/A）。");

                return;

            }

            if (outEdit->text().trimmed().isEmpty()) {

                QMessageBox::warning(&dlg, "输出为空", "请填写输出 JSON 路径。");

                return;

            }

            dlg.accept();

        });

        if (dlg.exec() != QDialog::Accepted) return;

        QString input = resolveInput(inputEdit->text().trimmed());

        QString out = outEdit->text().trimmed();

        if (!out.endsWith(".json", Qt::CaseInsensitive)) out += ".json";

        out = QFileInfo(out).absoluteFilePath();

        const QString species = speciesBox->currentData().toString();

        const int gt = gtSpin->value();

        const bool useBoard = boardChk->isChecked();

        // 表型 JSON 和对比 JSON 放同一目录：工作图 / pl_gray 图按帧号命名，

        // 表型页就是按「JSON 同目录」找 raw_bgr_000000.png / pl_gray_000000.png

        QString phenoOut;

        if (phenoChk->isChecked()) {

            const QFileInfo oi(out);

            phenoOut = oi.absolutePath() + "/" + oi.completeBaseName()

                       + "_phenotyping.json";

        }

        QStringList args{

            QFileInfo(runtimeScript).absoluteFilePath(),

            "--species", species,

            "--out", out};

        if (!phenoOut.isEmpty()) args << "--pheno-out" << phenoOut;

        if (QFileInfo(input).isDir()) args << "--dir" << input;

        else args << "--image" << input;

        if (gt >= 0) args << "--gt" << QString::number(gt);

        Q_UNUSED(useBoard); // This dialog only invokes the local algorithm runtime.

        QDir repoDir(QFileInfo(runtimeScript).absolutePath());

        repoDir.cdUp();

        repoDir.cdUp();

        genProcess.setWorkingDirectory(repoDir.absolutePath());

        QProcessEnvironment env = QProcessEnvironment::systemEnvironment();

        env.insert("PYTHONUTF8", "1");

        env.insert("PYTHONIOENCODING", "utf-8");

        genProcess.setProcessEnvironment(env);

        genOutPath = out;

        genPhenoPath = phenoOut;

        genSwitchTab = true;

        genQuiet = false;

        genTag = "生成";

        genBusyOwner = false;

        cmpGenerate->setEnabled(false);

        cmpGenerate->setText("生成中…");

        append(QString("生成对比：%1（%2 / %3%4%5）")

                   .arg(QFileInfo(input).fileName(),

                        speciesBox->currentText(),

                        useBoard ? "经板端" : "本机算法",

                        gt >= 0 ? QString(" / GT %1").arg(gt) : QString(),

                        phenoOut.isEmpty() ? QString()

                                           : QString(" / 同时生成表型")));

        genProcess.start(python->text().trimmed(), args);

    });

    // 生成进程统一按 UTF-8 解码（启动时已设 PYTHONIOENCODING/PYTHONUTF8），

    // 否则中文日志在 GBK 控制台上会变成乱码

    QObject::connect(&genProcess, &QProcess::readyReadStandardOutput, [&]() {

        const QString text =

            QString::fromUtf8(genProcess.readAllStandardOutput());

        // 实时分析每 2~5 秒一轮，进度行全部打出来会把运行日志冲掉；

        // 只要读走管道（避免子进程阻塞），失败时再由退出码和 stderr 报

        if (genLive) return;

        for (const QString &line : text.split('\n', Qt::SkipEmptyParts)) {

            const QString trimmed = line.trimmed();

            if (!trimmed.isEmpty()) append("[" + genTag + "] " + trimmed);

        }

    });

    QObject::connect(&genProcess, &QProcess::readyReadStandardError, [&]() {

        const QString text =

            QString::fromUtf8(genProcess.readAllStandardError());

        for (const QString &line : text.split('\n', Qt::SkipEmptyParts)) {

            const QString trimmed = line.trimmed();

            if (!trimmed.isEmpty()) append("[" + genTag + "] " + trimmed);

        }

    });

    QObject::connect(&genProcess, &QProcess::errorOccurred, [&](QProcess::ProcessError e) {

        if (e != QProcess::FailedToStart) return;

        if (genBusyOwner) {

            genBusyOwner = false;

            if (setBusyFn) setBusyFn(false);

        }

        if (genLive) {

            liveAnalyze.stop();

            genLive = false;

            append("实时分析已停止：启动不了 Python（" + python->text()

                   + "）。检查「高级设置」里的 Python 路径。");

        } else {

            cmpGenerate->setEnabled(true);

            cmpGenerate->setText("生成对比…");

            append(genTag + "失败：启动不了 Python（" + python->text()

                   + "）。检查「高级设置」里的 Python 路径。");

        }

    });

    QObject::connect(&genProcess,

                     QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished),

                     [&](int code, QProcess::ExitStatus) {

        // 先把本轮参数拷出来：下面会复位共享状态，实时分析可能紧接着开下一轮

        const bool wasLive = genLive;

        const bool wantPheno = genWantPheno;

        const bool wantCmp = genWantCmp;

        const QString outPath = genOutPath;

        const QString phenoPath = genPhenoPath;

        const bool switchTab = genSwitchTab;

        const bool quiet = genQuiet;

        genLive = false;

        genWantPheno = true;

        genWantCmp = true;

        genSwitchTab = true;

        genQuiet = false;

        genPhenoPath.clear();

        if (!wasLive) {

            cmpGenerate->setEnabled(true);

            cmpGenerate->setText("生成对比…");

        }

        if (genBusyOwner) {

            genBusyOwner = false;

            if (setBusyFn) setBusyFn(false);

        }

        if (code != 0) {

            if (wasLive)

                append(QString("实时分析：本轮退出码 %1，下一轮重试").arg(code));

            else

                append(QString("%1结束：退出码 %2，未载入结果").arg(genTag).arg(code));

            return;

        }

        // 实时自动分析：只刷新勾选的结果，绝不切页签、不改 busy。

        // 表型载入后暂停 live 检测覆盖（否则 200ms 后又被检测框冲掉），

        // 但采集进程继续跑，下一轮分析照常取最新帧。

        if (wasLive) {

            bool loaded = false;

            if (wantPheno && !phenoPath.isEmpty()) {

                if (QFileInfo::exists(phenoPath)) {

                    // 首轮写日志；之后每 2 秒一轮，重复的「载入…」会刷屏，静默刷新

                    const QString perr = loadJsonPath(phenoPath, true, liveResultLogged);

                    if (perr.isEmpty()) {

                        liveShowAnalysis = true;

                        loaded = true;

                    } else {

                        append("实时分析：单粒表型载入失败：" + perr);

                    }

                } else {

                    append("实时分析：没找到单粒表型输出 " + phenoPath);

                }

            }

            if (wantCmp) {

                if (!outPath.isEmpty() && QFileInfo::exists(outPath)) {

                    loadCmpFromUi(outPath, false, true, true);

                    loaded = true;

                } else {

                    append("实时分析：没找到对比输出 " + outPath);

                }

            }

            if (loaded && !liveResultLogged) {

                liveResultLogged = true;

                append("实时分析：首轮结果已载入，之后自动刷新"

                       "（切到对应页签即可查看）");

            }

            return;

        }

        // 非实时：只载入勾选的结果；没勾的那一页保留上一次的结果

        if (wantPheno && !phenoPath.isEmpty()) {

            if (QFileInfo::exists(phenoPath)) {

                const QString perr = loadJsonPath(phenoPath);

                append(perr.isEmpty()

                           ? "单粒表型结果已载入：" + phenoPath

                           : "单粒表型结果载入失败：" + perr);

            } else {

                append("没找到单粒表型输出 " + phenoPath);

            }

        }

        if (wantCmp) {

            if (outPath.isEmpty() || !QFileInfo::exists(outPath)) {

                append(genTag + "完成，但没找到输出文件 " + outPath);

                return;

            }

            loadCmpFromUi(outPath, switchTab, quiet);

        }

    });

    QObject::connect(cmpTable, &QTableWidget::itemSelectionChanged, [&]() {

        if (selectionSync) return;

        cmpSelectObject(cmpTable->currentRow());

    });

    // 模式切换：只换显示，不动数据（数据三种模式都在 JSON 里，早就有了）

    QObject::connect(cmpModeGroup, &QButtonGroup::idClicked, [&](int id) {

        cmpMode = id;

        cmpSelected = -1;

        cmpRebuild();

        append(QString("对比：分离模式切到「%1」").arg(cmpModeLabel[id - 1]));

    });

    auto stepImage = [&](int delta) {

        if (cmpListMap.isEmpty()) return;

        int row = cmpListMap.indexOf(cmpIndex);

        if (row < 0) row = 0;

        row = (row + delta + cmpListMap.size()) % cmpListMap.size();

        cmpSelectRow(row);

        append(QString("对比：%1 → 第 %2/%3 张")

                   .arg(delta > 0 ? "下一张" : "上一张")

                   .arg(row + 1).arg(cmpListMap.size()));

    };

    QObject::connect(cmpNext, &QPushButton::clicked, [&]() { stepImage(1); });

    QObject::connect(cmpPrev, &QPushButton::clicked, [&]() { stepImage(-1); });

    // ---------------- 列表 / 详情卡 ----------------

    fillTable = [&]() {

        selectionSync = true;

        // 检测关掉时不展示上一轮/载入文件里的目标：图像区已经显示空状态，

        // 右栏还列着 10 粒会让人以为这些结果是这次跑出来的

        const int rowCount = detect->isChecked() ? int(objects.size()) : 0;

        table->setRowCount(rowCount);

        for (int i = 0; i < rowCount; ++i) {

            const SeedObject &o = objects[i];

            QString tag = displayTag(o);

            if (!o.status.isEmpty() && o.status != "OK") tag = "⚠ " + tag;

            auto *c0 = new QTableWidgetItem(tag);

            auto *c1 = new QTableWidgetItem(QString::number(o.confidence, 'f', 2));

            auto *c2 = new QTableWidgetItem(o.hasMetrics

                ? QString::number(o.lengthPx, 'f', 1) : "--");

            auto *c3 = new QTableWidgetItem(o.hasMetrics

                ? QString::number(o.widthPx, 'f', 1) : "--");

            auto *c4 = new QTableWidgetItem(o.hasMetrics

                ? QString::number(o.areaPx, 'f', 0) : "--");

            c0->setForeground(statusColor(o.status));

            for (QTableWidgetItem *it : {c1, c2, c3, c4})

                it->setTextAlignment(Qt::AlignRight | Qt::AlignVCenter);

            table->setItem(i, 0, c0);

            table->setItem(i, 1, c1);

            table->setItem(i, 2, c2);

            table->setItem(i, 3, c3);

            table->setItem(i, 4, c4);

        }

        selectionSync = false;

    };

    updateConsistency = [&]() {

        const FrameData *fd = current();

        if (!fd || objects.isEmpty() || !detect->isChecked()) {

            // 区分三种“空”：还没跑、检测被关掉、跑了但没找到目标。

            // 都写「尚未载入对象」会把关检测说成文件没载入，和图像区的空状态对不上。

            count->setText("--");

            consistency->setText(!fd ? QStringLiteral("尚未载入对象")

                               : detect->isChecked()

                                   ? QStringLiteral("未检测到目标")

                                   : QStringLiteral("目标检测已关闭（只跑 FPGA 链路）"));

            consistency->setStyleSheet("color:#5c7a63;");

            return;

        }

        const int declared = fd->seedCount < 0 ? int(objects.size()) : fd->seedCount;

        const bool match = (declared == objects.size());

        // C2 要求：Seed Count 必须等于目标列表行数，这里把两个数并排摆出来

        consistency->setText(match ? QString("Seed Count %1 = 列表 %2 · 一致 ✓")

                                         .arg(declared).arg(objects.size())

                                   : QString("⚠ Seed Count %1 ≠ 列表 %2 · 不一致")

                                         .arg(declared).arg(objects.size()));

        consistency->setToolTip(match

            ? QString("Seed Count（结果自带 %1）与列表行数（%2）一致")

                  .arg(declared).arg(objects.size())

            : QString("Seed Count（结果自带 %1）与列表行数（%2）不一致")

                  .arg(declared).arg(objects.size()));

        consistency->setStyleSheet(match ? "color:#1e7d34;font-weight:700;"

                                         : "color:#b3411f;font-weight:700;");

        count->setText(QString::number(objects.size()));

    };

    updateDetail = [&]() {

        if (!detect->isChecked() || selectedObj < 0 || selectedObj >= objects.size()) {

            for (QLabel *v : detailValues) v->setText("—");

            statusBadge->setText("无选中");

            statusBadge->setStyleSheet("background:#9aa995;color:white;border-radius:6px;"

                                       "padding:4px 8px;font-weight:700;");

            reason->clear();

            return;

        }

        const SeedObject &o = objects[selectedObj];

        if (!o.hasMetrics) {   // live 检测框：没有表型数值就如实显示 —

            for (QLabel *v : detailValues) v->setText("—");

        } else {

            detailValues[0]->setText(QString::number(o.areaPx, 'f', 1));

            detailValues[1]->setText(QString::number(o.lengthPx, 'f', 2));

            detailValues[2]->setText(QString::number(o.widthPx, 'f', 2));

            detailValues[3]->setText(QString::number(o.perimeterPx, 'f', 2));

            detailValues[4]->setText(QString::number(o.aspectRatio, 'f', 3));

            detailValues[5]->setText(QString::number(o.circularity, 'f', 4));

        }

        const QString st = o.status.isEmpty() ? "--" : o.status;

        statusBadge->setText(st);

        statusBadge->setStyleSheet(

            QString("background:%1;color:white;border-radius:6px;"

                    "padding:4px 8px;font-weight:700;").arg(statusColor(o.status).name()));

        reason->setText(o.statusReason.isEmpty()

                            ? QString() : "原因：" + o.statusReason);

    };

    selectObject = [&](int index) {

        if (index < -1 || index >= objects.size()) return;

        if (selectedObj == index) return;

        selectedObj = index;

        updateDetail();

        if (index >= 0 && !selectionSync) {

            selectionSync = true;

            table->selectRow(index);

            table->scrollToItem(table->item(index, 0));

            selectionSync = false;

        }

        redraw();

    };

    auto switchFrame = [&](int index) {

        if (index < 0 || index >= frames.size()) return;

        currentFrame = index;

        objects = frames[index].objects;

        selectedObj = -1;

        // 程序化切帧（--frame / --export-all）时下拉框也要跟着走，

        // 但不能再触发一次 currentIndexChanged，否则选中项被重置

        if (frameBox->currentIndex() != index) {

            const QSignalBlocker blocker(frameBox);

            frameBox->setCurrentIndex(index);

        }

        fillTable();

        updateConsistency();

        updateDetail();

        if (!objects.isEmpty()) selectObject(0);

        resetZoom();       // 换了帧就复位缩放，否则看到的是上一张的放大位置

        redraw();

        status->setText(QString("已载入检测结果 · 帧 %1 · 目标 %2 · 单位 px / px²")

                            .arg(frames[index].frameId).arg(objects.size()));

    };

    // ---------------- 导出 ----------------

    // 一键导出（界面按钮 / --export / --export-all 共用同一条代码路径）

    exportBatchTo =

        [&](const QString &rootDirPath, int frameIndex) -> QString {

        if (frameIndex < 0 || frameIndex >= frames.size())

            return "没有可导出的帧";

        const FrameData &fd = frames[frameIndex];

        const QString dir = rootDirPath + "/"

            + QDateTime::currentDateTime().toString("yyyyMMdd_HHmmss")

            + QString("_batch_%1").arg(++batchSeq, 3, 10, QChar('0'));

        if (!QDir().mkpath(dir))

            return "无法创建目录：" + dir;

        bool ok = true;

        if (!fd.original.isNull()) ok = fd.original.save(dir + "/original.png") && ok;

        if (!fd.fpga.isNull()) ok = fd.fpga.save(dir + "/fpga.png") && ok;

        if (!fd.original.isNull()) ok = drawBoxes(fd.original, true).save(dir + "/annotated.png") && ok;

        if (!fd.fpga.isNull()) ok = drawPheno(fd.fpga).save(dir + "/phenotyping.png") && ok;

        auto write = [&](const QString &name, const QByteArray &data) {

            QSaveFile file(dir + "/" + name);

            if (!file.open(QIODevice::WriteOnly)) return false;

            if (file.write(data) != data.size()) return false;

            return file.commit();

        };

        QString csv = "seed_id,confidence,length_px,width_px,area_px,"

                      "perimeter_px,aspect_ratio,circularity,status,"

                      "bbox_x1,bbox_y1,bbox_x2,bbox_y2\n";

        for (const SeedObject &o : fd.objects) {

            csv += QString("%1,%2,%3,%4,%5,%6,%7,%8,%9,%10,%11,%12,%13\n")

                       .arg(o.id)

                       .arg(o.confidence, 0, 'f', 4)

                       .arg(o.lengthPx, 0, 'f', 2)

                       .arg(o.widthPx, 0, 'f', 2)

                       .arg(o.areaPx, 0, 'f', 2)

                       .arg(o.perimeterPx, 0, 'f', 2)

                       .arg(o.aspectRatio, 0, 'f', 4)

                       .arg(o.circularity, 0, 'f', 4)

                       .arg(o.status)

                       .arg(o.bbox.x(), 0, 'f', 1)

                       .arg(o.bbox.y(), 0, 'f', 1)

                       .arg(o.bbox.x() + o.bbox.width(), 0, 'f', 1)

                       .arg(o.bbox.y() + o.bbox.height(), 0, 'f', 1);

        }

        ok = write("results.csv", csv.toUtf8()) && ok;

        QJsonArray objectsJson;

        for (const SeedObject &o : fd.objects) {

            QJsonObject j{{"id", o.id},

                          {"confidence", o.confidence},

                          {"bbox", QJsonArray{o.bbox.x(), o.bbox.y(),

                                              o.bbox.x() + o.bbox.width(),

                                              o.bbox.y() + o.bbox.height()}},

                          {"area_px", o.areaPx},

                          {"length_px", o.lengthPx},

                          {"width_px", o.widthPx},

                          {"perimeter_px", o.perimeterPx},

                          {"aspect_ratio", o.aspectRatio},

                          {"circularity", o.circularity},

                          {"status", o.status}};

            if (!o.seedIdText.isEmpty()) j.insert("seed_id", o.seedIdText);

            if (!o.statusReason.isEmpty()) j.insert("status_reason", o.statusReason);

            objectsJson.append(j);

        }

        QJsonObject out{{"schema_version", 1},

                        {"frame_id", fd.frameId},

                        {"seed_count", fd.objects.size()},

                        {"unit", QJsonObject{{"length", "px"}, {"width", "px"},

                                             {"perimeter", "px"}, {"area", "px^2"}}},

                        {"source_json", loadedJson},

                        {"exported_at", QDateTime::currentDateTime().toString(

                             "yyyy-MM-dd HH:mm:ss")},

                        {"images", QJsonObject{{"original", "original.png"},

                                               {"fpga", "fpga.png"},

                                               {"annotated", "annotated.png"},

                                               {"phenotyping", "phenotyping.png"}}},

                        {"objects", objectsJson}};

        ok = write("objects.json", QJsonDocument(out).toJson()) && ok;

        append((ok ? "批次已导出：" : "导出不完整，请检查磁盘：") + dir);

        return ok ? dir : ("导出不完整：" + dir);

    };

    // ---------------- 后端（真链路） ----------------

    auto setBusy = [&](bool value) {

        busy = value;

        // 不要把 livePage/imgPage/batchPage 整页禁用：实时分析在独立进程里跑，

        // 采集和「停止」必须一直可用。页面里的输入控件逐个列在下面。

        QList<QWidget *> lock{source, sizeBox, framesSpin, host,

                              python, script, pickImage, pickDir, detect, detector,

                              species, gtSpin, phenoChk, cmpChk, conf, openJson};

        for (QPushButton *b : segBtns) lock << b;

        for (QWidget *w : lock) w->setEnabled(!value);

        // GT 只属于单张图片模式：解锁后也要按当前模式恢复，不能一律打开

        if (!value) gtSpin->setEnabled(modeValue == "Image");

        start->setEnabled(!value);

        stop->setEnabled(value && !stopping);

        restart->setEnabled(value && !stopping);

        save->setEnabled(!value && !frames.isEmpty());

    };

    // 生成/分析进程比后端早定义，结束时通过这个名字解锁界面

    setBusyFn = setBusy;

    // 模式 / 数据源都从状态变量取（原来是读下拉框的 currentText，

    // 换成分段按钮后没有控件可读）

    auto backendRequest = [&]() {

        const QString src = source->currentData().toString();

        QJsonObject req{{"mode", modeValue},

                        {"source", src},

                        {"output_dir", runDir},

                        {"width", runWidth},

                        {"height", runHeight},

                        {"frames", framesSpin->value()},

                        {"host", host->text()},

                        {"port", cli.value(optPort).toInt()},

                        {"threshold", 40},

                        {"morph", 1},

                        {"gauss", true},

                        {"detect", detect->isChecked()},

                        {"conf", conf->value() / 100.0},

                        {"detector", detector->currentText()},

                        {"species", species->currentData().toString()},

                        {"live_compare", cmpChk->isChecked()},

                        {"comparison_interval", 1.0},

                        {"mock_fps", 8.0}};

        req.insert("net_port", netPort->value());

        // FPGA 处理模式：板卡源走 live_backend 的 preset（板端会按它写寄存器）

        req.insert("preset", fpgaPreset->currentData().toString());

        if (modeValue == "Batch") req.insert("input_dir", inputPath);

        else if (modeValue == "Image") req.insert("image_path", inputPath);

        else if (!inputPath.isEmpty() && src == "mock")

            req.insert("input_dir", inputPath);

        return req;

    };

    launch = [&]() {

        if (process.state() != QProcess::NotRunning) return;

        if (genProcess.state() != QProcess::NotRunning) {

            append("上一轮分析还在跑，等它结束再开始");

            return;

        }

        // 单张 / 批量：直接走统一分析（冻结检测器），只产出勾选的结果。

        // 不再先起实时后端，避免同一张图被检测两遍、表格被 live 轮询覆盖。

        if ((modeValue == "Image" || modeValue == "Batch")

            && detect->isChecked()) {

            const bool wantPheno = phenoChk->isChecked();

            const bool wantCmp = cmpChk->isChecked();

            if (!wantPheno && !wantCmp) {

                QMessageBox::warning(

                    &win, "没有勾选结果",

                    "请至少勾选「生成单粒表型结果」或「生成粘连分离结果」之一。");

                return;

            }

            QString input = inputPath;

            if (!QFileInfo::exists(input)) {

                const QString found = findRepoFile(input);

                if (!found.isEmpty()) input = found;

            }

            const bool isDir = (modeValue == "Batch");

            if (input.isEmpty() || !QFileInfo::exists(input)

                || QFileInfo(input).isDir() != isDir) {

                QMessageBox::warning(

                    &win, "输入未选择",

                    isDir ? "请先在左侧「采集与检测」里选择一个存在的目录。"

                          : "请先在左侧「采集与检测」里选择一张存在的图片。");

                return;

            }

            startUnifiedAnalysis(input, isDir,

                                 species->currentData().toString(),

                                 gtSpin->value(),

                                 source->currentData().toString() == "board",

                                 false, wantPheno, wantCmp, false, "分析");

            return;

        }

        if (!QFileInfo::exists(script->text())) {

            QMessageBox::warning(&win, "缺少后端", "找不到 " + script->text());

            return;

        }

        QDir().mkpath(runDir);

        QFile::remove(runDir + "/stop.request");

        QFile::remove(runDir + "/live_status.json");

        QSaveFile requestFile(runDir + "/request.json");

        if (!requestFile.open(QIODevice::WriteOnly)

            || requestFile.write(QJsonDocument(backendRequest()).toJson()) < 0

            || !requestFile.commit()) {

            QMessageBox::warning(&win, "文件错误", "请求 JSON 保存失败");

            return;

        }

        showLive = true;

        lastLiveFrame = -1;

        lastLiveSignature.clear();

        liveNeedsRebuild = true;

        output.clear();

        // 新一轮采集：先回到实时预览，第一轮分析出结果后再切到结果画面

        liveShowAnalysis = false;

        liveAnalyzedStamp = -1;

        liveResultLogged = false;

        append(QString("启动后端：%1（%2 / %3 / 检测=%4 / 阈值=%5）")

                   .arg(QFileInfo(script->text()).fileName(), modeValue,

                        source->currentData().toString(),

                        detect->isChecked() ? "开" : "关",

                        QString::number(conf->value() / 100.0, 'f', 2)));

        process.setWorkingDirectory(QFileInfo(script->text()).absolutePath());

        process.start(python->text().trimmed(),

                      {QFileInfo(script->text()).absoluteFilePath(),

                       "--request", runDir + "/request.json"});

        poll.start();

        captureClock.restart();

        if (!capturePath.isEmpty()) captureWatch.start();

        const bool wantPheno = phenoChk->isChecked();

        const bool wantCmp = cmpChk->isChecked();

        if (detect->isChecked() && (wantPheno || wantCmp)) {

            liveAnalyze.start();

            QStringList what;

            if (wantPheno) what << "单粒表型";

            if (wantCmp) what << "粘连分离";

            append("实时分析：自动分析最新帧，产出 " + what.join(" + ")

                   + "（采集与停止始终可用）");

        } else {

            append("实时分析：未启用（需打开目标检测并至少勾选一项结果）");

        }

        setBusy(true);

    };

    auto stopBackend = [&]() {

        liveAnalyze.stop();

        liveAnalyzedStamp = -1;

        if (process.state() == QProcess::NotRunning || stopping) return;

        stopping = true;

        QFile marker(runDir + "/stop.request");

        marker.open(QIODevice::WriteOnly);

        marker.write("stop");

        marker.close();

        process.terminate();

        append("已请求停止");

        // 分析进程可能正持有 busy：这时别提前解锁，等分析进程结束再说

        if (!genBusyOwner) setBusy(false);

        stop->setEnabled(false);

        restart->setEnabled(false);

    };

    auto shutdownBackend = [&]() {

        if (process.state() == QProcess::NotRunning) return;

        stopping = true;

        QFile marker(runDir + "/stop.request");

        marker.open(QIODevice::WriteOnly);

        marker.write("stop");

        marker.close();

        process.terminate();

        if (!process.waitForFinished(5000)) {

            process.kill();

            process.waitForFinished(3000);

        }

    };

    // ---------------- 交互 ----------------

    QObject::connect(openJson, &QPushButton::clicked, [&]() {

        const QString path = QFileDialog::getOpenFileName(

            &win, "载入表型结果 JSON", QDir::currentPath(), "JSON (*.json)");

        if (path.isEmpty()) return;

        const QString err = loadJsonPath(path);

        if (!err.isEmpty()) QMessageBox::warning(&win, "载入失败", err);

    });

    QObject::connect(frameBox, QOverload<int>::of(&QComboBox::currentIndexChanged),

                     [&](int index) { switchFrame(index); });

    QObject::connect(table, &QTableWidget::itemSelectionChanged, [&]() {

        if (selectionSync) return;

        const auto rows = table->selectionModel()->selectedRows();

        if (!rows.isEmpty()) selectObject(rows.first().row());

    });

    for (int i = 0; i < viewList.size(); ++i) {

        ClickableLabel *view = viewList[i];

        view->onClicked = [&, i](QPoint pos) {

            if (objects.isEmpty()) return;

            if (i != 2 && i != 3) return;

            const ViewState &vs = viewStates[i];

            if (vs.composite.isNull() || vs.scale <= 0) return;

            const QPointF imgPt = (QPointF(pos) - vs.offset) / vs.scale;

            QPointF origPt = imgPt;

            const FrameData *fd = current();

            if (i == 3 && fd && !fd->original.isNull() && !fd->fpga.isNull()) {

                origPt = QPointF(imgPt.x() * fd->original.width() / double(fd->fpga.width()),

                                 imgPt.y() * fd->original.height() / double(fd->fpga.height()));

            }

            int best = -1;

            double bestArea = 0;

            for (int k = 0; k < objects.size(); ++k) {

                const QRectF &b = objects[k].bbox;

                if (b.contains(origPt)) {

                    const double a = b.width() * b.height();

                    if (best < 0 || a < bestArea) {

                        best = k;

                        bestArea = a;

                    }

                }

            }

            if (best >= 0) {

                selectObject(best);

                append(QString("点击选中 %1（视图 %2）")

                           .arg(displayTag(objects[best])).arg(viewTitles[i]));

            }

        };

    }

    QObject::connect(save, &QPushButton::clicked, [&]() {

        if (currentFrame < 0) return;

        const QString rootDirPath = QFileDialog::getExistingDirectory(

            &win, "选择导出根目录", QDir::currentPath());

        if (rootDirPath.isEmpty()) return;

        const QString out = exportBatchTo(rootDirPath, currentFrame);

        QMessageBox::information(&win,

            out.contains("不") || out.contains("无法") ? "导出提示" : "导出完成", out);

    });

    QObject::connect(start, &QPushButton::clicked, [&]() { launch(); });

    QObject::connect(stop, &QPushButton::clicked, [&]() { stopBackend(); });

    QObject::connect(restart, &QPushButton::clicked, [&]() {

        pendingRestart = true;

        stopBackend();

    });

    // —— network：网络摄像头源 ——

    // 连接 = 起后端、监听端口；断开 = 停后端。开始/停止仍控制整条处理链路。

    QObject::connect(netConnect, &QPushButton::clicked, [&]() {

        if (source->currentData().toString() != "net") {

            append("当前数据源不是「网络摄像头」，先在左侧把数据源切到网络摄像头");

            return;

        }

        launch();

    });

    QObject::connect(netDisconnect, &QPushButton::clicked, [&]() { stopBackend(); });

    auto syncNetControls = [&]() {

        const bool isNet = source->currentData().toString() == "net";

        const bool isBoard = source->currentData().toString() == "board";

        const bool running = process.state() != QProcess::NotRunning;

        netConnect->setEnabled(isNet && !running);

        netDisconnect->setEnabled(isNet && running);

        netStatus->setText(isNet ? "网络：未启动（点「连接网络」开始监听）"

                                 : "网络：当前数据源不是网络摄像头");

        fpgaPreset->setEnabled(isBoard);

        if (!isBoard)

            fpgaStatus->setText(isNet ? "FPGA：未参与（网络摄像头链路不经过 PYNQ）"

                                      : "FPGA：当前数据源不是板卡实时");

    };

    QObject::connect(source, &QComboBox::currentIndexChanged,

                     [&](int) { syncNetControls(); });

    QObject::connect(&process, &QProcess::stateChanged,

                     [&](QProcess::ProcessState) { syncNetControls(); });

    syncNetControls();

    // 实时页：采集不停，每轮取最新预览帧跑一次统一分析。

    // 分析在独立 QProcess 里，比采集慢，所以用「上一轮结束 + 2 秒后再看」的节奏，

    // 不做逐帧推理；结果出来后由 finished 处理器静默刷新两个页签。

    liveAnalyzeOnce = [&]() {

        if (!showLive) return;

        if (process.state() == QProcess::NotRunning) {

            liveAnalyze.stop();

            return;

        }

        if (genProcess.state() != QProcess::NotRunning) return;  // 上一轮未结束

        if (!detect->isChecked()) return;

        const bool wantPheno = phenoChk->isChecked();

        const bool wantCmp = cmpChk->isChecked();

        if (!wantPheno && !wantCmp) return;

        // 网络源由后端直接复用当前 YOLO 结果生成三路对比，避免每轮

        // 重新加载模型；Qt 只轮询新的 comparison.json 并刷新页面。

        if (source->currentData().toString() == "net" && wantCmp) {

            const QString root = QDir(outDirEdit->text()).absolutePath();

            const QString cmpPath =

                QDir(root).absoluteFilePath("analysis/comparison.json");

            const QFileInfo cmpInfo(cmpPath);

            if (!cmpInfo.exists()) return;

            const qint64 stamp =

                cmpInfo.lastModified().toMSecsSinceEpoch() * 1000 + cmpInfo.size();

            if (stamp == liveAnalyzedStamp) return;

            const QString err = loadComparison(cmpPath);

            if (!err.isEmpty()) {

                if (!liveResultLogged)

                    append("实时对比载入失败：" + err);

                return;

            }

            liveAnalyzedStamp = stamp;

            cmpOnlyFail->setChecked(false);

            cmpFillList();

            if (cmpCombo->count() > 0) cmpSelectRow(0);

            if (!liveResultLogged) {

                append("实时粘连对比已开始自动刷新");

                liveResultLogged = true;

            }

            return;

        }

        const QString root = QDir(outDirEdit->text()).absolutePath();

        QString latest;

        for (const char *name : {"cam_bgr_latest.png", "cam_latest.png"}) {

            const QString p =

                QDir(root).absoluteFilePath(QString("preview/") + name);

            if (QFileInfo::exists(p)) { latest = p; break; }

        }

        if (latest.isEmpty()) return;           // 首帧还没落盘

        const QFileInfo fi(latest);

        const qint64 stamp =

            fi.lastModified().toMSecsSinceEpoch() * 1000 + fi.size();

        if (stamp == liveAnalyzedStamp) return; // 这一帧已经分析过

        // 用 QImage 读写而不是 QFile::copy：后端可能正在覆盖写，

        // 读失败就跳过这一拍，绝不把半张图送进算法

        QImageReader reader(latest);

        reader.setAutoTransform(true);

        const QImage frame = reader.read();

        if (frame.isNull()) return;

        QDir().mkpath(root + "/analysis");

        const QString snapshot = root + "/analysis/live_snapshot.png";

        if (!frame.save(snapshot, "PNG")) {

            append("实时分析：快照保存失败，下一轮重试");

            return;

        }

        liveAnalyzedStamp = stamp;

        startUnifiedAnalysis(snapshot, false,

                             species->currentData().toString(), -1,

                             false, false, wantPheno, wantCmp, true, "实时分析");

    };

    QObject::connect(&liveAnalyze, &QTimer::timeout, &win,

                     [&]() { liveAnalyzeOnce(); });

    QObject::connect(detect, &QCheckBox::toggled, [&](bool on) {

        append(on ? "目标检测已打开" : "目标检测已关闭（只跑 FPGA 链路）");

        // 立即同步四视图与右栏：关检测后 ③④ 变空状态、列表清空，

        // 不用等下一次运行，屏幕上不会出现“空状态 + 10 粒结果”的矛盾画面

        redraw();

        fillTable();

        updateConsistency();

        updateDetail();

        if (process.state() != QProcess::NotRunning) {

            pendingRestart = true;

            stopBackend();

        }

    });

    QObject::connect(conf, &QSlider::valueChanged, [&](int v) {

        confLabel->setText(QString("置信度阈值 %1").arg(v / 100.0, 0, 'f', 2));

    });

    QObject::connect(folder, &QPushButton::clicked, [&]() {

        QDesktopServices::openUrl(QUrl::fromLocalFile(runDir));

    });

    QObject::connect(&process, &QProcess::readyReadStandardOutput, [&]() {

        output += process.readAllStandardOutput();

    });

    QObject::connect(&process, &QProcess::readyReadStandardError, [&]() {

        append(QString::fromUtf8(process.readAllStandardError()).trimmed());

    });

    QObject::connect(&process, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished),

                     [&](int code, QProcess::ExitStatus) {

        // 后端可能在两次 200ms 轮询之间就跑完了；退出时补读一次，

        // 否则 Image/Batch 的最终结果图会永远不出现。

        if (showLive) pollStatus();

        poll.stop();

        captureWatch.stop();

        liveAnalyze.stop();

        append(QString("后端退出，code=%1").arg(code));

        // stdout 以前只收集不显示，出错时只剩一个 code=1。现在收进运行日志。

        if (!output.isEmpty())

            append("后端输出：" + QString::fromUtf8(output).trimmed());

        // 若「分析当前帧」已接手 busy，保持锁定到分析结束

        if (!genBusyOwner) setBusy(false);

        const bool requestedStop = stopping;

        stopping = false;

        if (pendingRestart) {

            pendingRestart = false;

            QTimer::singleShot(400, &win, launch);

        } else if (!requestedStop && backendCrashRestarts < 3) {

            ++backendCrashRestarts;

            append(QString("后端意外退出，%1 秒后自动重启（%2/3）")

                       .arg(1).arg(backendCrashRestarts));

            QTimer::singleShot(1000, &win, launch);

        }

    });

    // live 轮询：只有"显示 live 结果"时才覆盖界面，载入的表型结果不会被冲掉

    pollStatus = [&]() {

        if (!showLive) return;

        QFile file(runDir + "/live_status.json");

        if (!file.open(QIODevice::ReadOnly)) return;

        const QByteArray raw = file.readAll();
        file.close();

        QJsonParseError error;

        const QJsonDocument doc = QJsonDocument::fromJson(raw, &error);

        if (error.error != QJsonParseError::NoError || !doc.isObject()) return;

        const QJsonObject rootObj = doc.object();
        if (rootObj.value("state").toString() == "ERROR") {
            frames.clear(); objects.clear();
            status->setText("ERROR | " + rootObj.value("error").toString());
            metrics->setText("OFFLINE | previous results invalidated");
            redraw();
            return;
        }

        const QJsonObject ai = rootObj.value("ai").toObject();

        const QJsonObject preview = rootObj.value("preview").toObject();

        const QJsonObject latency = rootObj.value("latency").toObject();

        const QJsonObject fps = rootObj.value("fps").toObject();

        const QJsonObject counters = rootObj.value("counters").toObject();

        QJsonValue frameId = rootObj.contains("frame_id") ? rootObj.value("frame_id")

                                                          : rootObj.value("frame");

        lastLiveFrame = frameId.toInt();

        const int seedCount = ai.value("seed_count").toInt();

        // —— network：网络摄像头状态（电脑1 → 电脑2）——

        {

            const QJsonObject network = rootObj.value("network").toObject();

            if (network.contains("listen_port")) {

                const QString state = rootObj.value("state").toString();

                const bool connected = network.value("connected").toBool();

                QString text = QString("网络：%1 · 端口 %2")

                        .arg(connected ? "已连接"

                                       : state == "LISTENING" ? "等待电脑1推帧…"

                                       : state == "DISCONNECTED" ? "已断开（等待重连）"

                                                                 : state)

                        .arg(network.value("listen_port").toInt());

                if (connected)

                    text += QString(" · 对端 %1").arg(network.value("peer").toString());

                const double netFps = network.value("fps").toDouble();

                if (netFps > 0)

                    text += QString(" · %1 fps").arg(netFps, 0, 'f', 1);

                const int framesReceived = network.value("frames_received").toInt();

                if (framesReceived > 0)

                    text += QString(" · 收 %1 帧").arg(framesReceived);

                const int reconnects = network.value("reconnects").toInt();

                if (reconnects > 0)

                    text += QString(" · 重连 %1 次").arg(reconnects);

                const int dropped = network.value("dropped").toInt();

                if (dropped > 0)

                    text += QString(" · 丢旧帧 %1").arg(dropped);

                const QString netError = network.value("last_error").toString();

                if (!netError.isEmpty())

                    text += " · " + netError;

                netStatus->setText(text);

                netStatus->setStyleSheet(connected ? "color:#1b7f3a;" : "color:#b06000;");

            }

        }

        // —— network：FPGA 实际回执（板卡源才有）——

        {

            const QString fpgaState = rootObj.value("fpga").toString();

            const QString dmaState = rootObj.value("dma").toString();

            const QString appliedPreset = rootObj.value("preset").toString();

            if (!fpgaState.isEmpty() || !appliedPreset.isEmpty()) {

                QString text = "FPGA：" + (fpgaState.isEmpty() ? QString("—") : fpgaState);

                if (!dmaState.isEmpty())

                    text += " · DMA：" + dmaState;

                if (!appliedPreset.isEmpty())

                    text += " · 模式：" + appliedPreset;

                fpgaStatus->setText(text);

                fpgaStatus->setStyleSheet(fpgaState == "ONLINE" ? "color:#1b7f3a;"

                                                                : "color:#b06000;");

            }

        }

        QVector<SeedObject> dets;

        for (const QJsonValue &value : ai.value("detections").toArray()) {

            const QJsonObject item = value.toObject();

            const QJsonArray bbox = item.value("bbox_xyxy").toArray();

            if (bbox.size() != 4) continue;

            SeedObject det;

            det.id = item.value("id").toInt();

            det.confidence = item.value("confidence").toDouble();

            det.bbox = QRectF(bbox[0].toDouble(), bbox[1].toDouble(),

                              bbox[2].toDouble() - bbox[0].toDouble(),

                              bbox[3].toDouble() - bbox[1].toDouble());

            det.status = "";            // live 只有检测框，没有表型与状态判定

            det.hasMetrics = false;

            dets.append(det);

        }

        // 判断「要不要重建列表」看检测内容，不看帧号：

        // mock 回放每帧帧号都涨，拿帧号当签名会每 200ms 清空重填一次表格，

        // 选中和滚动位置跟着跳，看起来就是一闪一闪。

        QString detSignature;

        for (const SeedObject &d : dets)

            detSignature += QString("%1:%2:%3:%4:%5:%6|")

                                .arg(d.id)

                                .arg(qRound(d.bbox.x())).arg(qRound(d.bbox.y()))

                                .arg(qRound(d.bbox.width())).arg(qRound(d.bbox.height()))

                                .arg(qRound(d.confidence * 100.0));

        const bool detChanged = detSignature != lastLiveSignature;

        const bool firstPoll = liveNeedsRebuild;

        const bool rebuild = detChanged || firstPoll;

        const QString liveText = QString("第 %1 帧").arg(lastLiveFrame);

        // 预览图可能正被后端写一半（尤其本地回放 8fps 覆盖写）；

        // 读失败时保留上一张，不要把视图清空。后端已改成原子写，这里再兜一层。

        auto loadImg = [](const QString &path, const QImage &fallback) {

            if (path.isEmpty()) return fallback;

            QImageReader reader(path);

            reader.setAutoTransform(true);

            const QImage image = reader.read();

            return image.isNull() ? fallback : image;

        };

        // 实时分析结果正在显示时，顶部 FPS/状态照常更新，但不再用

        // 每 200ms 的检测框覆盖表型结果图和目标列表 —— 否则结果刚载入就被冲掉

        const bool holdAnalysis = liveShowAnalysis;

        if (!holdAnalysis) {

        if (rebuild) {

            liveNeedsRebuild = false;

            lastLiveSignature = detSignature;

            // 重建前记住用户点中的目标 id，重建后按 id 选回来

            const int keepId = (selectedObj >= 0 && selectedObj < objects.size())

                                   ? objects[selectedObj].id : -1;

            // 第一轮不要把上一份 JSON 的图当兜底，否则会短暂显示旧结果

            const QImage prevOriginal = firstPoll || frames.isEmpty()

                ? QImage() : frames[0].original;

            const QImage prevFpga = firstPoll || frames.isEmpty()

                ? QImage() : frames[0].fpga;

            FrameData fd;

            fd.frameId = lastLiveFrame;

            fd.seedCount = seedCount;

            fd.objects = dets;

            fd.original = loadImg(preview.value("cam").toString(), prevOriginal);

            fd.fpga = loadImg(preview.value("board").toString(), prevFpga);

            frames = QVector<FrameData>{fd};

            currentFrame = 0;

            if (frameBox->count() == 0) {

                frameBox->addItem(liveText);

                frameBox->setEnabled(false);

            } else if (frameBox->itemText(0) != liveText) {

                frameBox->setItemText(0, liveText);   // 只改文字，不 clear/add

            }

            objects = fd.objects;

            selectedObj = -1;

            const int scroll = table->verticalScrollBar()->value();

            fillTable();

            table->verticalScrollBar()->setValue(scroll);

            updateConsistency();

            bool reselected = false;

            if (keepId >= 0) {

                for (int k = 0; k < objects.size(); ++k) {

                    if (objects[k].id == keepId) {

                        selectObject(k);

                        reselected = true;

                        break;

                    }

                }

            }

            if (!reselected) updateDetail();

        } else {

            // 检测没变、但图仍在更新：只换图，不动列表/选中

            if (!frames.isEmpty() && currentFrame == 0) {

                frames[0].original = loadImg(preview.value("cam").toString(),

                                             frames[0].original);

                frames[0].fpga = loadImg(preview.value("board").toString(),

                                         frames[0].fpga);

            }

            if (frameBox->count() > 0 && frameBox->itemText(0) != liveText)

                frameBox->setItemText(0, liveText);

        }

        }   // !holdAnalysis

        modelState->setText("MODEL " + (ai.value("model_state").toString().isEmpty()

                                            ? "OFFLINE"

                                            : ai.value("model_state").toString()));

        inferLabel->setText("AI 推理 " + QString::number(ai.value("infer_ms").toDouble(), 'f', 3)

                            + " ms" + (ai.value("enabled").toBool(false) ? "" : "（已关闭）"));

        const bool running = rootObj.value("running").toBool();

        const QString stateText = rootObj.value("state").toString();

        const QString head = stateText == "DONE" ? QStringLiteral("完成")

                           : stateText == "STOPPED" ? QStringLiteral("已停止")

                           : stateText == "ERROR" ? QStringLiteral("ERROR")

                           : QStringLiteral("LIVE");

        const QString cameraText = rootObj.contains("camera")

            ? rootObj.value("camera").toString()

            : (running ? QStringLiteral("ONLINE") : QStringLiteral("--"));

        QString statusLine = QString("%1 | FRAME %2 | CAMERA %3 | FPGA %4 | DMA %5 | MODEL %6 | 检测框 %7")

                                 .arg(head)

                                 .arg(lastLiveFrame)

                                 .arg(cameraText)

                                 .arg(rootObj.value("fpga").toString("--"))

                                 .arg(rootObj.value("dma").toString("--"))

                                 .arg(ai.value("model_state").toString("--"))

                                 .arg(dets.size());

        const QString errorText = rootObj.value("error").toString();

        if (!errorText.isEmpty()) statusLine += " | " + errorText;

        status->setText(statusLine);

        const QJsonValue cameraFps = fps.contains("camera") ? fps.value("camera")

                                                            : rootObj.value("camera_fps");

        const QJsonValue liveFps = fps.contains("live") ? fps.value("live")

                                                        : rootObj.value("live_fps");

        metrics->setText(QString("Camera FPS %1 | Live FPS %2 | RTT %3 ms | AI 推理 %4 ms"

                                 " | OK %5 / 超时 %6 / DMA %7")

                             .arg(QString::number(cameraFps.toDouble(), 'f', 1))

                             .arg(QString::number(liveFps.toDouble(), 'f', 1))

                             .arg(QString::number(latency.value("rtt_ms").toDouble(), 'f', 2))

                             .arg(QString::number(ai.value("infer_ms").toDouble(), 'f', 3))

                             .arg(counters.value("ok").toInt())

                             .arg(counters.value("timeout").toInt())

                             .arg(counters.value("dma_err").toInt()));

        if (!holdAnalysis) redraw();

    };

    QObject::connect(&poll, &QTimer::timeout, [&]() { pollStatus(); });

    // --capture：等"有画面 + 列表与检测数一致"再截（live 证据图用）

    QObject::connect(&captureWatch, &QTimer::timeout, [&]() {

        if (captureDone) return;

        const bool enoughFrames = lastLiveFrame >= 5 || !showLive;

        const bool ready = enoughFrames && !frames.isEmpty()

                           && !frames[0].original.isNull()

                           && table->rowCount() == int(objects.size())

                           && (detect->isChecked() ? !objects.isEmpty() : true);

        const bool backendGone = (process.state() == QProcess::NotRunning);

        if (!ready && !backendGone && captureClock.elapsed() < captureTimeout) return;

        captureDone = true;

        captureWatch.stop();

        redraw();

        const bool saved = win.grab().save(capturePath);

        append((saved ? "已截图：" : "截图失败：") + capturePath

               + QString("（检测框 %1 个）").arg(objects.size()));

        QTimer::singleShot(300, &app, [&, saved]() {

            shutdownBackend();

            app.exit(saved ? 0 : 2);

        });

    });

    QTimer refresh;

    QObject::connect(&refresh, &QTimer::timeout, [&]() {

        if (!busy && !liveShowAnalysis) redraw();

        // Comparison 视图只在控件尺寸变了时才重画（拖分割条、改窗口）

        if (!cmpImages.isEmpty()

            && (cmpLeftView->contentsRect().size() != cmpLastSize[0]

                || cmpRightView->contentsRect().size() != cmpLastSize[1]))

            cmpPresentAll();

    });

    refresh.start(400);

    // 结果在后台载入时，目标页签可能正被隐藏：隐藏页的控件尺寸还没算出来，

    // presentImage 会跳过，切过去就只看到旧的空画面。切页时补一次重绘，

    // 并用 0ms 延迟再补一次（等 QTabWidget 完成页面布局）。

    auto refreshTabContent = [&](int idx) {

        if (idx == 0) {

            redraw();

        } else if (idx == 1 && cmpIndex >= 0 && cmpIndex < cmpImages.size()) {

            cmpRebuild();

        }

    };

    QObject::connect(tabs, &QTabWidget::currentChanged, &win, [&, refreshTabContent](int idx) {

        refreshTabContent(idx);

        QTimer::singleShot(0, &win, [&, idx]() { refreshTabContent(idx); });

    });

    append("就绪。点「载入检测结果」查看表型测量（多帧可切换）；"

           "或点「开始运行」执行一次检测链路。");

    // ---------------- 启动参数动作 ----------------

    // segmentation Comparison：载入对比 JSON -> 选模式 -> 选图 -> （可选）连点/点选自检

    if (!cmpPath.isEmpty()) {

        QTimer::singleShot(0, [&]() {

            tabs->setCurrentIndex(1);

            const QString err = loadComparison(cmpPath);

            if (!err.isEmpty()) {

                append("对比结果载入失败：" + err);

                return;

            }

            append(QString("对比结果载入：%1（%2 张图）")

                       .arg(QFileInfo(cmpPath).fileName()).arg(cmpImages.size()));

            if (!cmpSourceNote.isEmpty()) append("数据来源说明：" + cmpSourceNote);

            cmpMode = cli.value(optCmpMode).startsWith("fixed") ? 1 : 2;

            (cmpMode == 1 ? cmpBtnFixed : cmpBtnAdaptive)->setChecked(true);

            cmpOnlyFail->setChecked(cli.isSet(optCmpBad));

            cmpFillList();                       // setChecked 会触发过滤，这里兜一次底

            if (cmpCombo->count() == 0) {

                append("对比：筛选后没有图（--cmp-bad 时说明这批没有失败案例）");

            } else {

                int row = cli.value(optCmpIndex).toInt();

                if (row < 0 || row >= cmpCombo->count()) row = 0;

                cmpSelectRow(row);               // 行没变时也会补一次 cmpRebuild

            }

            // --cmp-click：合成一次点击轮廓（坐标反算自检）

            if (!cli.value(optCmpClick).isEmpty()) {

                QTimer::singleShot(250, &win, [&]() {

                    const int want = cli.value(optCmpClick).toInt();

                    if (cmpIndex < 0 || cmpIndex >= cmpImages.size()) return;

                    const CmpMode &cm = cmpImages[cmpIndex].modes[cmpMode];

                    int idx = -1;

                    for (int i = 0; i < cm.objects.size(); ++i)

                        if (cm.objects[i].id == want) idx = i;

                    if (idx < 0) {

                        append(QString("--cmp-click %1：当前图/模式里没有这个 id（共 %2 个实例）")

                                   .arg(want).arg(cm.objects.size()));

                        return;

                    }

                    const ViewState &vs = cmpRightState;

                    if (vs.scale <= 0) { append("--cmp-click：视图还没就绪"); return; }

                    const QPoint pos = (cm.objects[idx].labelPos * vs.scale + vs.offset).toPoint();

                    cmpSelectObject(-1);

                    cmpRightView->onClicked(pos);

                    append(QString("--cmp-click %1：合成点击 (%2,%3) -> 选中 %4")

                               .arg(want).arg(pos.x()).arg(pos.y())

                               .arg(cmpSelected >= 0

                                        ? cm.objects[cmpSelected].tag() : QString("无")));

                });

            }

            // --cmp-steps：连点「下一张」N 次（连续操作不卡死自检）

            const QString stepsText = cli.value(optCmpNext);

            if (!stepsText.isEmpty()) {

                const int n = qMax(1, stepsText.toInt());

                auto *seq = new QTimer(&win);

                auto *k = new int(0);

                seq->setInterval(80);

                append(QString("--cmp-steps %1：开始连点「下一张」").arg(n));

                QObject::connect(seq, &QTimer::timeout, [&, seq, k, n]() {

                    if (*k >= n) {

                        seq->stop();

                        append(QString("--cmp-steps：连点 %1 次完成，界面存活（当前 %2，模式 %3）")

                                   .arg(n)

                                   .arg(cmpIndex >= 0 ? cmpImages[cmpIndex].imageId

                                                      : QString("--"))

                                   .arg(cmpModeLabel[cmpMode]));

                        return;

                    }

                    ++(*k);

                    cmpNext->click();

                });

                QTimer::singleShot(250, seq, [seq]() { seq->start(); });

            }

        });

    }

    if (!loadPath.isEmpty()) {

        QTimer::singleShot(0, [&]() {

            const QString err = loadJsonPath(loadPath);

            if (!err.isEmpty()) {

                append("启动参数载入失败：" + err);

                return;

            }

            append("启动参数载入：" + loadPath);

            const int wantFrame = cli.value(optFrame).toInt();

            if (wantFrame > 0) {

                if (wantFrame < frames.size()) {

                    switchFrame(wantFrame);

                    append(QString("--frame %1：已切到帧 %2")

                               .arg(wantFrame).arg(frames[wantFrame].frameId));

                } else {

                    append(QString("--frame %1 超出范围（共 %2 帧）")

                               .arg(wantFrame).arg(frames.size()));

                }

            }

        });

    }

    if (!exportDir.isEmpty() || !exportAllDir.isEmpty()) {

        QTimer::singleShot(700, [&]() {

            if (frames.isEmpty()) {

                append("导出跳过：还没有数据（先 --load 一个结果 JSON）");

                return;

            }

            if (!exportAllDir.isEmpty()) {

                for (int i = 0; i < frames.size(); ++i) {

                    switchFrame(i);

                    append(QString("导出帧 %1：%2")

                               .arg(frames[i].frameId)

                               .arg(exportBatchTo(exportAllDir, i)));

                }

            } else {

                append("自动导出：" + exportBatchTo(exportDir, currentFrame));

            }

        });

    }

    // --click-seed / --select-row 自检：等数据到位再合成动作（live 要等第一帧）

    auto runWhenReady = [&](const QString &what, std::function<bool()> action) {

        auto *timer = new QTimer(&win);

        auto *attempts = new int(0);

        timer->setInterval(300);

        QObject::connect(timer, &QTimer::timeout, [&, timer, attempts, what, action]() {

            if (objects.isEmpty()) {

                if (++(*attempts) > 25) {

                    append(what + "：等不到目标数据，跳过");

                    timer->stop();

                }

                return;

            }

            timer->stop();

            if (!action()) append(what + "：找不到指定 Seed ID");

        });

        timer->start();

    };

    const QString clickSeed = cli.value(optClickSeed);

    if (!clickSeed.isEmpty()) {

        runWhenReady("--click-seed " + clickSeed, [&, clickSeed]() {

            const int id = clickSeed.toInt();

            int idx = -1;

            for (int k = 0; k < objects.size(); ++k)

                if (objects[k].id == id) idx = k;

            if (idx < 0) return false;

            const ViewState &vs = viewStates[2];

            const QPoint pos = (objects[idx].bbox.center() * vs.scale + vs.offset).toPoint();

            selectObject(-1);

            viewList[2]->onClicked(pos);

            append(QString("--click-seed %1：合成点击 (%2,%3) -> 选中 %4")

                       .arg(id).arg(pos.x()).arg(pos.y())

                       .arg(selectedObj >= 0 ? displayTag(objects[selectedObj])

                                             : QString("无")));

            return true;

        });

    }

    const QString selectRow = cli.value(optSelectRow);

    if (!selectRow.isEmpty()) {

        runWhenReady("--select-row " + selectRow, [&, selectRow]() {

            const int id = selectRow.toInt();

            int idx = -1;

            for (int k = 0; k < objects.size(); ++k)

                if (objects[k].id == id) idx = k;

            if (idx < 0) return false;

            selectObject(-1);

            table->selectRow(idx);

            append(QString("--select-row %1：选中第 %2 行 -> 详情卡 %3")

                       .arg(id).arg(idx)

                       .arg(selectedObj >= 0 ? displayTag(objects[selectedObj])

                                             : QString("无")));

            return true;

        });

    }

    // ---- Image 模式连续操作自检（任务书 C2：「10 次连续操作无卡死」）----

    // 一轮里交替做三件事：切帧 / 选表格行 / 合成点击图上目标。

    // 比单纯连点同一处更能碰出问题：三条路径都会改选中态并触发重绘与回填。

    const QString imgSteps = cli.value(optImgSteps);

    if (!imgSteps.isEmpty()) {

        runWhenReady("--img-steps " + imgSteps, [&, imgSteps]() {

            const int n = qMax(1, imgSteps.toInt());

            auto *seq = new QTimer(&win);

            auto *k = new int(0);

            seq->setInterval(120);

            append(QString("--img-steps %1：Image 模式连续操作自检开始（帧 %2 个，当前帧 %3 粒）")

                       .arg(n).arg(frames.size()).arg(objects.size()));

            QObject::connect(seq, &QTimer::timeout, [&, seq, k, n]() {

                if (*k >= n) {

                    seq->stop();

                    append(QString("--img-steps：%1 次完成，界面存活（帧 %2，共 %3 粒，选中 %4）")

                               .arg(n).arg(currentFrame).arg(objects.size())

                               .arg(selectedObj >= 0 ? displayTag(objects[selectedObj])

                                                     : QString("无")));

                    return;

                }

                const int i = (*k)++;

                switchFrame(i % frames.size());

                if (!objects.isEmpty()) {

                    const int r = i % objects.size();

                    table->selectRow(r);

                    const ViewState &vs = viewStates[2];

                    viewList[2]->onClicked(

                        (objects[r].bbox.center() * vs.scale + vs.offset).toPoint());

                }

            });

            QTimer::singleShot(400, seq, [seq]() { seq->start(); });

            return true;

        });

    }

    // ---- 看图放大自检 ----

    // --view-zoom "i:z"：把第 i 个视图设成 z 倍（截「就地放大」的证据图）

    // --inspect [i]    ：启动就开大窗（截网格放大窗的证据图），开哪页跟随当前页签

    const QString viewZoom = cli.value(optViewZoom);

    const bool wantInspect = cli.isSet(optInspect);

    if (!viewZoom.isEmpty() || wantInspect) {

        // 不能直接用 runWhenReady：那一套等的是表型页的 objects，

        // 而对比页载入后 objects 一直是空的

        auto *vt = new QTimer(&win);

        auto *tries = new int(0);

        vt->setInterval(250);

        QObject::connect(vt, &QTimer::timeout, [&, vt, tries, viewZoom, wantInspect]() {

            if (objects.isEmpty() && cmpIndex < 0) {

                if (++(*tries) > 24) {

                    vt->stop();

                    append("--view-zoom / --inspect：等不到图像，跳过");

                }

                return;

            }

            vt->stop();

            if (!viewZoom.isEmpty()) {

                const int i = viewZoom.section(':', 0, 0).toInt();

                const double z = viewZoom.section(':', 1, 1).toDouble();

                if (i < 0 || i >= 4 || z <= 0) {

                    append(QString("--view-zoom 参数应为 \"i:z\"（0≤i≤3, z>0），收到：")

                               + viewZoom + "，已忽略");

                } else if (!viewStates[i].composite.isNull()) {

                    ViewState &vs = viewStates[i];

                    vs.userZoom = qBound(1.0, z, kMaxUserZoom);

                    presentImage(viewList[i], &vs);

                    viewFitBtns[i]->setEnabled(vs.userZoom > 1.001);

                    append(QString("--view-zoom：视图 %1「%2」设为 %3 倍")

                               .arg(i).arg(viewTitles[i]).arg(vs.userZoom, 0, 'f', 2));

                } else {

                    append(QString("--view-zoom：视图 %1 当前没有图像（该页还没载入）").arg(i));

                }

            }

            // 开哪个大窗跟随当前页签：演示截图时不用另外记参数

            if (wantInspect) openInspect(tabs->currentIndex() == 1,

                                         cli.value(optInspect).toInt());

        });

        vt->start();

    }

    if (!shotPath.isEmpty()) {

        // 截图要等「连续操作」跑完：默认 1500ms，带 steps 时按步数往后推，

        // 否则截到的是第 6 步而不是最后一步（踩过一次）

        int shotDelay = 1500;

        if (cli.isSet(optShotDelay))

            shotDelay = qMax(100, cli.value(optShotDelay).toInt());

        if (!imgSteps.isEmpty())

            shotDelay = 400 + qMax(1, imgSteps.toInt()) * 130 + 900;

        if (!cli.value(optCmpNext).isEmpty())

            shotDelay = qMax(shotDelay, 250 + qMax(1, cli.value(optCmpNext).toInt()) * 90 + 900);

        if (!viewZoom.isEmpty() || wantInspect) shotDelay = qMax(shotDelay, 2200);

        QTimer::singleShot(shotDelay, [&]() {

            // 大窗开着就截大窗：证据图要的是放大后的画面，不是背后那个主窗口

            QWidget *target = (inspectDlg && inspectDlg->isVisible())

                ? static_cast<QWidget *>(inspectDlg) : static_cast<QWidget *>(&win);

            append(target->grab().save(shotPath)

                       ? (target == &win ? "窗口截图：" : "放大窗截图：") + shotPath

                       : "窗口截图失败");

            if (cli.isSet(optQuit)) QApplication::quit();

        });

    } else if (cli.isSet(optQuit)) {

        QTimer::singleShot(2500, []() { QApplication::quit(); });

    }

    win.show();

    if (autoStart) QTimer::singleShot(600, &win, launch);

    // --analyze-live 自检：等首帧落盘后立即触发一轮实时分析

    if (cli.isSet(optAnalyzeLive)) {

        QTimer::singleShot(4000, &win, [&]() {

            if (!showLive) {

                append("--analyze-live：当前不是实时模式，跳过");

                return;

            }

            append("--analyze-live：触发一轮实时分析");

            liveAnalyzeOnce();

        });

    }

    // --tab-switch 自检：验证「切页签立即重绘」。可写多段，如 20000:1,24000:0

    for (const QString &item : cli.value(optTabSwitch).split(',', Qt::SkipEmptyParts)) {

        const int ms = item.section(':', 0, 0).trimmed().toInt();

        const int idx = item.section(':', 1, 1).trimmed().toInt();

        QTimer::singleShot(qMax(0, ms), &win, [&, idx]() {

            tabs->setCurrentIndex(idx);

            append(QString("--tab-switch：切到第 %1 个页签").arg(idx));

        });

    }

    QObject::connect(&app, &QCoreApplication::aboutToQuit, [&]() {

        shutdownBackend();

        if (genProcess.state() != QProcess::NotRunning) {

            genProcess.terminate();

            if (!genProcess.waitForFinished(2000)) genProcess.kill();

        }

    });

    return app.exec();

}



