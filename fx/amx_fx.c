/* amx_fx - the Audio Mixer's effects, as one LADSPA library.
 *
 * Every plugin is stereo (In L/In R -> Out L/Out R) and its first control is "Enabled":
 * switching it crossfades (10 ms) between the dry input and the effect, so effects can be
 * toggled live without clicks and without rebuilding the PipeWire filter-chain.
 *
 * Build:  gcc -O2 -fPIC -shared -o amx_fx.so amx_fx.c -lrnnoise -lm
 * (without RNNoise:  add -DAMX_NO_RNNOISE and drop -lrnnoise)
 */
#include <math.h>
#include <stdlib.h>
#include <string.h>
#ifdef __SSE__
#include <xmmintrin.h>
#endif
#ifndef AMX_NO_RNNOISE
#include <rnnoise.h>
#endif

/* ------------------------------------------------------------------ LADSPA ABI (ladspa.h 1.1) */
typedef float LADSPA_Data;
typedef void *LADSPA_Handle;
typedef int LADSPA_Properties;
typedef int LADSPA_PortDescriptor;
typedef int LADSPA_PortRangeHintDescriptor;
typedef struct { LADSPA_PortRangeHintDescriptor HintDescriptor; LADSPA_Data LowerBound, UpperBound; } LADSPA_PortRangeHint;
typedef struct _LADSPA_Descriptor {
    unsigned long UniqueID;
    const char *Label;
    LADSPA_Properties Properties;
    const char *Name, *Maker, *Copyright;
    unsigned long PortCount;
    const LADSPA_PortDescriptor *PortDescriptors;
    const char *const *PortNames;
    const LADSPA_PortRangeHint *PortRangeHints;
    void *ImplementationData;
    LADSPA_Handle (*instantiate)(const struct _LADSPA_Descriptor *, unsigned long);
    void (*connect_port)(LADSPA_Handle, unsigned long, LADSPA_Data *);
    void (*activate)(LADSPA_Handle);
    void (*run)(LADSPA_Handle, unsigned long);
    void (*run_adding)(LADSPA_Handle, unsigned long);
    void (*set_run_adding_gain)(LADSPA_Handle, LADSPA_Data);
    void (*deactivate)(LADSPA_Handle);
    void (*cleanup)(LADSPA_Handle);
} LADSPA_Descriptor;
#define P_IN 1
#define P_OUT 2
#define P_CTL 4
#define P_AUDIO 8
#define H_BELOW 1
#define H_ABOVE 2
#define H_TOGGLED 4
#define H_INTEGER 32
#define H_DEF_MIDDLE 0xC0

/* ------------------------------------------------------------------ helpers */
#define MAXC 48
#define MAXBUF 16
#define CHUNK 1024
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static inline float db2lin(float db) { return powf(10.f, db / 20.f); }
static inline float lin2db(float x) { return x > 1e-9f ? 20.f * log10f(x) : -180.f; }
static inline float clampf(float x, float a, float b) { return x < a ? a : x > b ? b : x; }
static inline float tc(float ms, float sr) { return ms <= 0.f ? 0.f : expf(-1.f / (ms * 0.001f * sr)); }

enum { BQ_PEAK, BQ_LOWSHELF, BQ_HIGHSHELF, BQ_HP, BQ_LP, BQ_BP, BQ_NOTCH, BQ_AP, BQ_OFF };
typedef struct { float b0, b1, b2, a1, a2, z1[2], z2[2]; } bq_t;

static void bq_set(bq_t *b, int type, float sr, float f, float q, float gdb) {
    f = clampf(f, 5.f, sr * 0.49f);
    q = q < 0.05f ? 0.05f : q;
    float A = powf(10.f, gdb / 40.f), w = 2.f * (float)M_PI * f / sr, cw = cosf(w), sw = sinf(w);
    float al = sw / (2.f * q), b0 = 1, b1 = 0, b2 = 0, a0 = 1, a1 = 0, a2 = 0, sA;
    switch (type) {
    case BQ_PEAK: b0 = 1 + al * A; b1 = -2 * cw; b2 = 1 - al * A; a0 = 1 + al / A; a1 = -2 * cw; a2 = 1 - al / A; break;
    case BQ_LOWSHELF: sA = 2 * sqrtf(A) * al;
        b0 = A * ((A + 1) - (A - 1) * cw + sA); b1 = 2 * A * ((A - 1) - (A + 1) * cw); b2 = A * ((A + 1) - (A - 1) * cw - sA);
        a0 = (A + 1) + (A - 1) * cw + sA; a1 = -2 * ((A - 1) + (A + 1) * cw); a2 = (A + 1) + (A - 1) * cw - sA; break;
    case BQ_HIGHSHELF: sA = 2 * sqrtf(A) * al;
        b0 = A * ((A + 1) + (A - 1) * cw + sA); b1 = -2 * A * ((A - 1) + (A + 1) * cw); b2 = A * ((A + 1) + (A - 1) * cw - sA);
        a0 = (A + 1) - (A - 1) * cw + sA; a1 = 2 * ((A - 1) - (A + 1) * cw); a2 = (A + 1) - (A - 1) * cw - sA; break;
    case BQ_HP: b0 = (1 + cw) / 2; b1 = -(1 + cw); b2 = (1 + cw) / 2; a0 = 1 + al; a1 = -2 * cw; a2 = 1 - al; break;
    case BQ_LP: b0 = (1 - cw) / 2; b1 = 1 - cw; b2 = (1 - cw) / 2; a0 = 1 + al; a1 = -2 * cw; a2 = 1 - al; break;
    case BQ_BP: b0 = al; b1 = 0; b2 = -al; a0 = 1 + al; a1 = -2 * cw; a2 = 1 - al; break;
    case BQ_NOTCH: b0 = 1; b1 = -2 * cw; b2 = 1; a0 = 1 + al; a1 = -2 * cw; a2 = 1 - al; break;
    case BQ_AP: b0 = 1 - al; b1 = -2 * cw; b2 = 1 + al; a0 = 1 + al; a1 = -2 * cw; a2 = 1 - al; break;
    default: break; /* BQ_OFF: identity */
    }
    b->b0 = b0 / a0; b->b1 = b1 / a0; b->b2 = b2 / a0; b->a1 = a1 / a0; b->a2 = a2 / a0;
}
static inline float bq_run(bq_t *b, int c, float x) {
    float y = b->b0 * x + b->z1[c];
    b->z1[c] = b->b1 * x - b->a1 * y + b->z2[c];
    b->z2[c] = b->b2 * x - b->a2 * y;
    return y;
}

/* ------------------------------------------------------------------ plugin framework */
typedef struct inst inst_t;
typedef struct {
    const char *label, *name;
    int nctl;
    const char *const *cn;        /* control names, [0] = "Enabled" */
    const float (*cr)[3];         /* min, max, default */
    size_t stsize;
    void (*init)(inst_t *);
    void (*run)(inst_t *, float *const in[2], float *out[2], unsigned n);
    void (*fini)(inst_t *);
} fxdef_t;

struct inst {
    const fxdef_t *d;
    float sr;
    float *port[4 + MAXC];
    float c[MAXC];
    float fade;                   /* 0 = dry, 1 = effect */
    float *buf[MAXBUF];
    size_t bufn[MAXBUF];
    int nbuf;
    float tmp[2][CHUNK];
    void *st;
};

static float *ibuf(inst_t *I, size_t n) {   /* zeroed buffer, freed and cleared with the instance */
    float *p = calloc(n, sizeof(float));
    if (I->nbuf < MAXBUF) { I->buf[I->nbuf] = p; I->bufn[I->nbuf++] = n; }
    return p;
}

static LADSPA_Handle fx_instantiate(const LADSPA_Descriptor *D, unsigned long sr) {
    inst_t *I = calloc(1, sizeof(inst_t));
    I->d = D->ImplementationData;
    I->sr = (float)sr;
    I->st = calloc(1, I->d->stsize ? I->d->stsize : 1);
    for (int i = 0; i < I->d->nctl; i++) I->c[i] = I->d->cr[i][2];
    if (I->d->init) I->d->init(I);
    return I;
}
static void fx_connect(LADSPA_Handle h, unsigned long p, LADSPA_Data *d) { ((inst_t *)h)->port[p] = d; }
static void fx_cleanup(LADSPA_Handle h) {
    inst_t *I = h;
    if (I->d->fini) I->d->fini(I);
    for (int i = 0; i < I->nbuf; i++) free(I->buf[i]);
    free(I->st);
    free(I);
}
static void fx_run(LADSPA_Handle h, unsigned long n) {
    inst_t *I = h;
#ifdef __SSE__
    unsigned csr = _mm_getcsr();
    _mm_setcsr(csr | 0x8040);      /* flush denormals to zero */
#endif
    for (int i = 0; i < I->d->nctl; i++) if (I->port[4 + i]) I->c[i] = *I->port[4 + i];
    int on = I->c[0] > 0.5f;
    if (on && I->fade == 0.f)       /* re-enabled: forget the old tails */
        for (int i = 0; i < I->nbuf; i++) memset(I->buf[i], 0, I->bufn[i] * sizeof(float));
    float step = 1.f / (0.010f * I->sr);
    unsigned long done = 0;
    while (done < n) {
        unsigned m = (unsigned)((n - done) > CHUNK ? CHUNK : (n - done));
        float *in[2] = {I->port[0] + done, I->port[1] + done};
        float *out[2] = {I->port[2] + done, I->port[3] + done};
        if (!on && I->fade <= 0.f) {
            for (int c = 0; c < 2; c++) if (out[c] != in[c]) memmove(out[c], in[c], m * sizeof(float));
        } else {
            float *t[2] = {I->tmp[0], I->tmp[1]};
            I->d->run(I, in, t, m);
            for (unsigned i = 0; i < m; i++) {
                I->fade = on ? fminf(1.f, I->fade + step) : fmaxf(0.f, I->fade - step);
                float g = I->fade;
                float o0 = in[0][i] + (t[0][i] - in[0][i]) * g;
                float o1 = in[1][i] + (t[1][i] - in[1][i]) * g;
                out[0][i] = fabsf(o0) < 64.f ? o0 : 0.f;     /* never pass NaN / inf / garbage on */
                out[1][i] = fabsf(o1) < 64.f ? o1 : 0.f;
            }
        }
        done += m;
    }
#ifdef __SSE__
    _mm_setcsr(csr);
#endif
}

/* convenience for effects with a dry/wet "Mix" control */
static inline float wet(float dry, float w, float mix) { return dry + (w - dry) * mix; }

/* ================================================================== standard chain */

/* ---- noise suppression (RNNoise) */
#define RN_FRAME 480
typedef struct {
#ifndef AMX_NO_RNNOISE
    DenoiseState *s[2];
#endif
    float in[2][RN_FRAME], out[2][RN_FRAME];
    int pos;
} denoise_t;
static void denoise_init(inst_t *I) {
#ifndef AMX_NO_RNNOISE
    denoise_t *S = I->st;
    S->s[0] = rnnoise_create(NULL);
    S->s[1] = rnnoise_create(NULL);
#else
    (void)I;
#endif
}
static void denoise_fini(inst_t *I) {
#ifndef AMX_NO_RNNOISE
    denoise_t *S = I->st;
    rnnoise_destroy(S->s[0]);
    rnnoise_destroy(S->s[1]);
#else
    (void)I;
#endif
}
static void denoise_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    denoise_t *S = I->st;
    for (unsigned i = 0; i < n; i++) {
        for (int c = 0; c < 2; c++) {
            S->in[c][S->pos] = in[c][i] * 32768.f;
            out[c][i] = S->out[c][S->pos] * (1.f / 32768.f);
        }
        if (++S->pos == RN_FRAME) {
            S->pos = 0;
#ifndef AMX_NO_RNNOISE
            for (int c = 0; c < 2; c++) rnnoise_process_frame(S->s[c], S->out[c], S->in[c]);
#else
            memcpy(S->out, S->in, sizeof S->out);
#endif
        }
    }
}
static const char *const denoise_cn[] = {"Enabled"};
static const float denoise_cr[][3] = {{0, 1, 0}};

/* ---- noise gate */
typedef struct { float env, gain; int hold; } gate_t;
static void gate_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    gate_t *S = I->st;
    float thr = db2lin(I->c[1]), floor_ = db2lin(I->c[2]);
    float at = tc(I->c[3], I->sr), rt = tc(I->c[5], I->sr);
    int hold = (int)(I->c[4] * 0.001f * I->sr);
    float er = tc(20.f, I->sr);
    if (S->gain == 0.f) S->gain = floor_;
    for (unsigned i = 0; i < n; i++) {
        float x = fmaxf(fabsf(in[0][i]), fabsf(in[1][i]));
        S->env = x > S->env ? x : S->env * er + x * (1 - er);
        float target;
        if (S->env >= thr) { S->hold = hold; target = 1.f; }
        else if (S->hold > 0) { S->hold--; target = 1.f; }
        else target = floor_;
        float k = target > S->gain ? at : rt;
        S->gain = target + (S->gain - target) * k;
        out[0][i] = in[0][i] * S->gain;
        out[1][i] = in[1][i] * S->gain;
    }
}
static const char *const gate_cn[] = {"Enabled", "Threshold", "Range", "Attack", "Hold", "Release"};
static const float gate_cr[][3] = {{0, 1, 0}, {-90, 0, -45}, {-90, 0, -60}, {0.1f, 100, 2}, {0, 1000, 120}, {5, 2000, 150}};

/* ---- low cut (24 dB/oct Butterworth high-pass) */
typedef struct { bq_t a, b; float f; } lowcut_t;
static void lowcut_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    lowcut_t *S = I->st;
    if (S->f != I->c[1]) {
        S->f = I->c[1];
        bq_set(&S->a, BQ_HP, I->sr, S->f, 0.5412f, 0);
        bq_set(&S->b, BQ_HP, I->sr, S->f, 1.3066f, 0);
    }
    for (int c = 0; c < 2; c++)
        for (unsigned i = 0; i < n; i++) out[c][i] = bq_run(&S->b, c, bq_run(&S->a, c, in[c][i]));
}
static const char *const lowcut_cn[] = {"Enabled", "Frequency"};
static const float lowcut_cr[][3] = {{0, 1, 0}, {20, 500, 80}};

/* ---- 3-band tone EQ */
typedef struct { bq_t b[3]; float k[3]; } eq3_t;
static void eq3_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    eq3_t *S = I->st;
    if (S->k[0] != I->c[1] || S->k[1] != I->c[2] || S->k[2] != I->c[3] || S->b[0].b0 == 0.f) {
        for (int j = 0; j < 3; j++) S->k[j] = I->c[1 + j];
        bq_set(&S->b[0], BQ_LOWSHELF, I->sr, 120, 0.707f, S->k[0]);
        bq_set(&S->b[1], BQ_PEAK, I->sr, 1000, 0.7f, S->k[1]);
        bq_set(&S->b[2], BQ_HIGHSHELF, I->sr, 6000, 0.707f, S->k[2]);
    }
    for (int c = 0; c < 2; c++)
        for (unsigned i = 0; i < n; i++)
            out[c][i] = bq_run(&S->b[2], c, bq_run(&S->b[1], c, bq_run(&S->b[0], c, in[c][i])));
}
static const char *const eq3_cn[] = {"Enabled", "Bass", "Mid", "Treble"};
static const float eq3_cr[][3] = {{0, 1, 0}, {-15, 15, 0}, {-15, 15, 0}, {-15, 15, 0}};

/* ---- 10-band parametric EQ.  Type: 0 off, 1 bell, 2 low shelf, 3 high shelf, 4 low cut, 5 high cut, 6 notch */
#define PEQ_N 10
typedef struct { bq_t b[PEQ_N]; float k[PEQ_N][4]; int typ[PEQ_N]; } peq_t;
static const int peq_map[] = {BQ_OFF, BQ_PEAK, BQ_LOWSHELF, BQ_HIGHSHELF, BQ_HP, BQ_LP, BQ_NOTCH};
static void peq_init(inst_t *I) {
    peq_t *S = I->st;
    for (int j = 0; j < PEQ_N; j++) { S->k[j][0] = -1; S->typ[j] = BQ_OFF; }
}
static void peq_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    peq_t *S = I->st;
    for (int j = 0; j < PEQ_N; j++) {
        const float *k = &I->c[1 + j * 4];
        if (memcmp(k, S->k[j], sizeof S->k[j])) {
            memcpy(S->k[j], k, sizeof S->k[j]);
            int t = (int)(k[0] + 0.5f);
            t = t < 0 ? 0 : t > 6 ? 6 : t;
            S->typ[j] = peq_map[t];
            bq_set(&S->b[j], S->typ[j], I->sr, k[1], k[3], k[2]);
        }
    }
    float g = db2lin(I->c[1 + PEQ_N * 4]);
    for (int c = 0; c < 2; c++)
        for (unsigned i = 0; i < n; i++) {
            float x = in[c][i];
            for (int j = 0; j < PEQ_N; j++) if (S->typ[j] != BQ_OFF) x = bq_run(&S->b[j], c, x);
            out[c][i] = x * g;
        }
}
#define PEQ_BAND(i) "Type " #i, "Freq " #i, "Gain " #i, "Q " #i
static const char *const peq_cn[] = {"Enabled", PEQ_BAND(1), PEQ_BAND(2), PEQ_BAND(3), PEQ_BAND(4), PEQ_BAND(5),
                                     PEQ_BAND(6), PEQ_BAND(7), PEQ_BAND(8), PEQ_BAND(9), PEQ_BAND(10), "Output"};
#define PEQ_R {0, 6, 0}, {20, 20000, 1000}, {-24, 24, 0}, {0.1f, 18, 1}
static const float peq_cr[][3] = {{0, 1, 0}, PEQ_R, PEQ_R, PEQ_R, PEQ_R, PEQ_R, PEQ_R, PEQ_R, PEQ_R, PEQ_R, PEQ_R, {-24, 24, 0}};

/* ---- de-esser (split band: only the sibilant band is turned down) */
typedef struct { bq_t hp; float f, env, gr; } deess_t;
static void deess_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    deess_t *S = I->st;
    if (S->f != I->c[1]) { S->f = I->c[1]; bq_set(&S->hp, BQ_HP, I->sr, S->f, 0.707f, 0); }
    float thr = I->c[2], maxr = I->c[3], at = tc(1.f, I->sr), rt = tc(80.f, I->sr), gt = tc(3.f, I->sr);
    for (unsigned i = 0; i < n; i++) {
        float h0 = bq_run(&S->hp, 0, in[0][i]), h1 = bq_run(&S->hp, 1, in[1][i]);
        float x = fmaxf(fabsf(h0), fabsf(h1));
        S->env = x > S->env ? x + (S->env - x) * at : x + (S->env - x) * rt;
        float over = lin2db(S->env) - thr;
        float want = over > 0 ? fminf(maxr, over * 0.75f) : 0.f;
        S->gr = want + (S->gr - want) * gt;
        float g = db2lin(-S->gr);
        out[0][i] = in[0][i] - h0 + h0 * g;
        out[1][i] = in[1][i] - h1 + h1 * g;
    }
}
static const char *const deess_cn[] = {"Enabled", "Frequency", "Threshold", "Amount"};
static const float deess_cr[][3] = {{0, 1, 0}, {2000, 12000, 6000}, {-60, 0, -30}, {0, 24, 8}};

/* ---- compressor */
typedef struct { float gr; } comp_t;
static void comp_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    comp_t *S = I->st;
    float thr = I->c[1], ratio = fmaxf(1.f, I->c[2]), at = tc(I->c[3], I->sr), rt = tc(I->c[4], I->sr);
    float knee = I->c[5], mk = I->c[6];
    if (I->c[7] > 0.5f) mk += -thr * (1.f - 1.f / ratio) * 0.5f;
    for (unsigned i = 0; i < n; i++) {
        float x = lin2db(fmaxf(fabsf(in[0][i]), fabsf(in[1][i])));
        float d = x - thr, want;
        if (2 * d < -knee) want = 0;
        else if (knee > 0 && 2 * fabsf(d) <= knee) want = (1 / ratio - 1) * (d + knee / 2) * (d + knee / 2) / (2 * knee);
        else want = (1 / ratio - 1) * d;
        want = -want;                                 /* gain reduction, dB >= 0 */
        S->gr = want + (S->gr - want) * (want > S->gr ? at : rt);
        float g = db2lin(mk - S->gr);
        out[0][i] = in[0][i] * g;
        out[1][i] = in[1][i] * g;
    }
}
static const char *const comp_cn[] = {"Enabled", "Threshold", "Ratio", "Attack", "Release", "Knee", "Makeup", "Auto Makeup"};
static const float comp_cr[][3] = {{0, 1, 0}, {-60, 0, -18}, {1, 20, 3}, {0.1f, 200, 10}, {10, 2000, 150}, {0, 12, 6}, {-12, 24, 0}, {0, 1, 1}};

/* ---- lookahead limiter (5 ms) */
#define LIM_LA_MS 5.f
typedef struct { float *d[2], *t; unsigned *qi; float *qv; int L, w, qh, qt, qn; float g; unsigned long n; } lim_t;
static void lim_init(inst_t *I) {
    lim_t *S = I->st;
    S->L = (int)(LIM_LA_MS * 0.001f * I->sr) + 1;
    S->d[0] = ibuf(I, S->L);
    S->d[1] = ibuf(I, S->L);
    S->qn = S->L + 2;
    S->qi = calloc(S->qn, sizeof(unsigned));
    S->qv = ibuf(I, S->qn);
    S->g = 1.f;
}
static void lim_fini(inst_t *I) { free(((lim_t *)I->st)->qi); }
static void lim_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    lim_t *S = I->st;
    float ceil_ = db2lin(I->c[1]), rt = tc(I->c[2], I->sr), at = tc(LIM_LA_MS * 0.2f, I->sr);
    for (unsigned i = 0; i < n; i++, S->n++) {
        float pk = fmaxf(fabsf(in[0][i]), fabsf(in[1][i]));
        float t = pk > ceil_ ? ceil_ / pk : 1.f;
        /* sliding minimum of t over the lookahead window (monotonic deque) */
        while (S->qh != S->qt && S->qv[(S->qt + S->qn - 1) % S->qn] >= t) S->qt = (S->qt + S->qn - 1) % S->qn;
        S->qv[S->qt] = t; S->qi[S->qt] = (unsigned)S->n; S->qt = (S->qt + 1) % S->qn;
        while ((unsigned)S->n - S->qi[S->qh] >= (unsigned)S->L) S->qh = (S->qh + 1) % S->qn;
        float m = S->qv[S->qh];
        S->g = m + (S->g - m) * (m < S->g ? at : rt);
        for (int c = 0; c < 2; c++) {
            float y = S->d[c][S->w] * S->g;
            S->d[c][S->w] = in[c][i];
            out[c][i] = clampf(y, -ceil_, ceil_);
        }
        S->w = (S->w + 1) % S->L;
    }
}
static const char *const lim_cn[] = {"Enabled", "Ceiling", "Release"};
static const float lim_cr[][3] = {{0, 1, 0}, {-24, 0, -1}, {10, 1000, 80}};

/* ---- loudness limiter / ear protection (BS.1770 K-weighted momentary loudness, cut only)
 * Two cuts, and the larger one applies:
 *  - steady: momentary loudness (400 ms window sliding in 10 ms steps) over the target, eased in
 *    over 150 ms and faded back up over 1.5 s, as a smooth loudness rider.
 *  - instant: a fast detector (LOUD_FAST_MS RMS) watches what is left after the steady cut.
 *    When that suddenly jumps more than LOUD_FAST_MARGIN dB over the target (a blast, not normal
 *    drum hits), the extra cut goes down at once (1 ms, just enough not to click), fully from
 *    LOUD_FAST_FULL dB over, is held for LOUD_HOLD_MS so the gain can't follow the waveform (that
 *    buzzes), and then only fades back up (1.5 s) while the steady cut takes over. */
#ifndef LOUD_FAST_MS
#define LOUD_FAST_MS 10.f
#endif
#ifndef LOUD_FAST_MARGIN
#define LOUD_FAST_MARGIN 8.f
#endif
#ifndef LOUD_FAST_FULL
#define LOUD_FAST_FULL 12.f
#endif
#ifndef LOUD_HOLD_MS
#define LOUD_HOLD_MS 400.f
#endif
#define LOUD_NB 40              /* 40 x 10 ms = 400 ms momentary window */
typedef struct {
    bq_t k1, k2; double acc, sum, fast; unsigned cnt, blk, hold; double blocks[LOUD_NB]; int bi, ready;
    float gr, gs, gf, want;
} loud_t;
static void loud_init(inst_t *I) {
    loud_t *S = I->st;
    bq_set(&S->k1, BQ_HIGHSHELF, I->sr, 1681.97f, 0.7072f, 4.0f);
    bq_set(&S->k2, BQ_HP, I->sr, 38.135f, 0.5003f, 0);
    S->blk = (unsigned)(I->sr * 0.01f);
}
static void loud_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    loud_t *S = I->st;
    float target = I->c[1], maxcut = I->c[2];
    float at = tc(150.f, I->sr), fat = tc(1.f, I->sr), rt = tc(1500.f, I->sr);
    float ceil_ = db2lin(-1.f), knee = ceil_ * 0.8f;
    double fa = 1.0 - tc(LOUD_FAST_MS, I->sr);
    unsigned holdn = (unsigned)(LOUD_HOLD_MS * 0.001f * I->sr);
    for (unsigned i = 0; i < n; i++) {
        double p = 0;
        for (int c = 0; c < 2; c++) {
            float k = bq_run(&S->k2, c, bq_run(&S->k1, c, in[c][i]));
            p += (double)k * k;
        }
        S->acc += p;
        S->fast += (p - S->fast) * fa;
        if (++S->cnt >= S->blk) {
            double blkms = S->acc / S->cnt;
            S->sum += blkms - S->blocks[S->bi];
            S->blocks[S->bi] = blkms;
            S->bi = (S->bi + 1) % LOUD_NB;
            S->acc = 0; S->cnt = 0;
            if (S->ready < LOUD_NB) S->ready++;
            if (S->sum < 0) S->sum = 0;          /* float drift */
            float L = -0.691f + 10.f * log10f((float)(S->sum / S->ready) + 1e-12f);
            S->want = L > target ? fminf(maxcut, L - target) : 0.f;
        }
        /* steady cut */
        S->gs = S->want + (S->gs - S->want) * (S->want > S->gs ? at : rt);
        /* instant cut: R = how far over the target the audio is after the steady cut (measured
         * against the steady cut, not the total, so the instant cut doesn't stop itself early) */
        float wf = 0.f;
        float R = -0.691f + 10.f * log10f((float)S->fast + 1e-12f) - target - S->gs;
        if (R > LOUD_FAST_MARGIN) {
            float extra = R >= LOUD_FAST_FULL ? R
                        : (R - LOUD_FAST_MARGIN) * LOUD_FAST_FULL / (LOUD_FAST_FULL - LOUD_FAST_MARGIN);
            wf = fminf(maxcut, S->gs + extra);
        }
        if (wf > S->gf) {
            S->gf = wf + (S->gf - wf) * fat;
            S->hold = holdn;
        } else if (S->hold) {
            S->hold--;
        } else {
            S->gf *= rt;
        }
        S->gr = fmaxf(S->gs, S->gf);
        float g = db2lin(-S->gr);
        for (int c = 0; c < 2; c++) {
            float y = in[c][i] * g, a = fabsf(y);
            if (a > knee) y = copysignf(knee + (ceil_ - knee) * tanhf((a - knee) / (ceil_ - knee)), y);
            out[c][i] = y;
        }
    }
}
static const char *const loud_cn[] = {"Enabled", "Target", "Max Cut"};
static const float loud_cr[][3] = {{0, 1, 0}, {-40, -5, -16}, {0, 40, 24}};

/* ================================================================== creative FX */

/* ---- creative EQ: amp-style Gain / Bass / Middle / Treble / Presence / Master */
typedef struct { bq_t b[4]; float k[5]; } ceq_t;
static void ceq_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    ceq_t *S = I->st;
    if (memcmp(S->k, &I->c[2], sizeof S->k) || S->b[0].b0 == 0.f) {
        memcpy(S->k, &I->c[2], sizeof S->k);
        bq_set(&S->b[0], BQ_LOWSHELF, I->sr, 120, 0.707f, (I->c[2] - 5) * 2.4f);
        bq_set(&S->b[1], BQ_PEAK, I->sr, 700, 0.8f, (I->c[3] - 5) * 2.4f);
        bq_set(&S->b[2], BQ_HIGHSHELF, I->sr, 3200, 0.707f, (I->c[4] - 5) * 2.4f);
        bq_set(&S->b[3], BQ_PEAK, I->sr, 5500, 1.2f, (I->c[5] - 5) * 2.0f);
    }
    float gain = I->c[1], d = 0.1f + gain * gain * 0.6f, nd = 1.f / tanhf(d), m = db2lin(I->c[6]);
    for (int c = 0; c < 2; c++)
        for (unsigned i = 0; i < n; i++) {
            float x = gain > 0.01f ? tanhf(in[c][i] * d) * nd : in[c][i];
            for (int j = 0; j < 4; j++) x = bq_run(&S->b[j], c, x);
            out[c][i] = x * m;
        }
}
static const char *const ceq_cn[] = {"Enabled", "Gain", "Bass", "Middle", "Treble", "Presence", "Master"};
static const float ceq_cr[][3] = {{0, 1, 0}, {0, 10, 0}, {0, 10, 5}, {0, 10, 5}, {0, 10, 5}, {0, 10, 5}, {-24, 12, 0}};

/* ---- overdrive */
typedef struct { bq_t pre, tone, dc; float k[2]; } od_t;
static void od_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    od_t *S = I->st;
    if (S->k[0] != I->c[2] || S->pre.b0 == 0.f) {
        S->k[0] = I->c[2];
        bq_set(&S->pre, BQ_HP, I->sr, 120, 0.707f, 0);
        bq_set(&S->tone, BQ_LP, I->sr, 900.f * powf(2.f, I->c[2] * 0.37f), 0.707f, 0);
        bq_set(&S->dc, BQ_HP, I->sr, 15, 0.707f, 0);
    }
    float g = db2lin(I->c[1] * 4.f), lvl = db2lin(I->c[3]);
    for (int c = 0; c < 2; c++)
        for (unsigned i = 0; i < n; i++) {
            float x = bq_run(&S->pre, c, in[c][i]) * g;
            x = x > 0 ? tanhf(x) : tanhf(x * 0.7f) / 0.7f * 0.85f;   /* asymmetric: a little even-order colour */
            x = bq_run(&S->dc, c, bq_run(&S->tone, c, x));
            out[c][i] = x * lvl * 0.7f;
        }
}
static const char *const od_cn[] = {"Enabled", "Drive", "Tone", "Level"};
static const float od_cr[][3] = {{0, 1, 0}, {0, 10, 5}, {0, 10, 5}, {-24, 6, -6}};

/* ---- tape saturator */
typedef struct { bq_t lo, hi, dc; float k; } tape_t;
static void tape_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    tape_t *S = I->st;
    if (S->k != I->c[2] || S->lo.b0 == 0.f) {
        S->k = I->c[2];
        bq_set(&S->lo, BQ_LOWSHELF, I->sr, 90, 0.707f, S->k * 3.f);
        bq_set(&S->hi, BQ_HIGHSHELF, I->sr, 7000, 0.707f, -S->k * 9.f);
        bq_set(&S->dc, BQ_HP, I->sr, 10, 0.707f, 0);
    }
    float g = db2lin(I->c[1]), comp = 1.f / sqrtf(g), mix = I->c[3], bias = 0.15f, tb = tanhf(bias);
    for (int c = 0; c < 2; c++)
        for (unsigned i = 0; i < n; i++) {
            float x = tanhf(in[c][i] * g + bias) - tb;
            x = bq_run(&S->dc, c, bq_run(&S->hi, c, bq_run(&S->lo, c, x))) * comp;
            out[c][i] = wet(in[c][i], x, mix);
        }
}
static const char *const tape_cn[] = {"Enabled", "Drive", "Warmth", "Mix"};
static const float tape_cr[][3] = {{0, 1, 0}, {0, 24, 9}, {0, 1, 0.5f}, {0, 1, 1}};

/* ---- bitcrusher */
typedef struct { float hold[2]; float cnt; } crush_t;
static void crush_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    crush_t *S = I->st;
    float bits = clampf(I->c[1], 1, 16), ds = fmaxf(1.f, I->c[2]), mix = I->c[3];
    float steps = powf(2.f, bits - 1);
    for (unsigned i = 0; i < n; i++) {
        if ((S->cnt += 1.f) >= ds) {
            S->cnt -= ds;
            for (int c = 0; c < 2; c++) S->hold[c] = roundf(in[c][i] * steps) / steps;
        }
        for (int c = 0; c < 2; c++) out[c][i] = wet(in[c][i], S->hold[c], mix);
    }
}
static const char *const crush_cn[] = {"Enabled", "Bits", "Downsample", "Mix"};
static const float crush_cr[][3] = {{0, 1, 0}, {1, 16, 8}, {1, 50, 4}, {0, 1, 1}};

/* ---- modulated delay line (chorus / flanger) */
typedef struct { float *b[2]; int n, w; double ph; float fb[2]; } mdl_t;
static inline float rd_interp(const float *b, int size, int w, float d) {
    float p = (float)w - d;
    while (p < 0) p += size;
    int i0 = (int)p, i1 = (i0 + 1) % size;
    float fr = p - i0;
    return b[i0 % size] * (1 - fr) + b[i1] * fr;
}
static void mdl_init(inst_t *I) {
    mdl_t *S = I->st;
    S->n = (int)(0.06f * I->sr) + 4;
    S->b[0] = ibuf(I, S->n);
    S->b[1] = ibuf(I, S->n);
}
static void chorus_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    mdl_t *S = I->st;
    float rate = I->c[1], depth = I->c[2] * 0.001f * I->sr, mix = I->c[3], base = 0.012f * I->sr;
    double inc = rate / I->sr;
    for (unsigned i = 0; i < n; i++) {
        S->ph += inc; if (S->ph >= 1) S->ph -= 1;
        for (int c = 0; c < 2; c++) {
            S->b[c][S->w] = in[c][i];
            float lfo = sinf(2.f * (float)M_PI * (float)(S->ph + c * 0.25));
            float y = rd_interp(S->b[c], S->n, S->w, base + depth * (0.5f + 0.5f * lfo));
            out[c][i] = wet(in[c][i], y, mix * 0.5f) * (1.f + mix * 0.2f);
        }
        S->w = (S->w + 1) % S->n;
    }
}
static const char *const chorus_cn[] = {"Enabled", "Rate", "Depth", "Mix"};
static const float chorus_cr[][3] = {{0, 1, 0}, {0.05f, 5, 0.8f}, {0, 10, 3}, {0, 1, 0.6f}};

static void flanger_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    mdl_t *S = I->st;
    float rate = I->c[1], depth = I->c[2] * 0.001f * I->sr, fb = I->c[3], mix = I->c[4], base = 0.0005f * I->sr;
    double inc = rate / I->sr;
    for (unsigned i = 0; i < n; i++) {
        S->ph += inc; if (S->ph >= 1) S->ph -= 1;
        for (int c = 0; c < 2; c++) {
            float lfo = sinf(2.f * (float)M_PI * (float)(S->ph + c * 0.25));
            float y = rd_interp(S->b[c], S->n, S->w, base + depth * (0.5f + 0.5f * lfo));
            S->b[c][S->w] = clampf(in[c][i] + y * fb, -4.f, 4.f);
            out[c][i] = wet(in[c][i], (in[c][i] + y) * 0.5f, mix);
        }
        S->w = (S->w + 1) % S->n;
    }
}
static const char *const flanger_cn[] = {"Enabled", "Rate", "Depth", "Feedback", "Mix"};
static const float flanger_cr[][3] = {{0, 1, 0}, {0.02f, 5, 0.25f}, {0, 8, 2}, {-0.95f, 0.95f, 0.5f}, {0, 1, 1}};

/* ---- phaser (6 all-pass stages) */
#define PH_ST 6
typedef struct { float z[2][PH_ST], last[2]; double ph; } phaser_t;
static void phaser_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    phaser_t *S = I->st;
    float rate = I->c[1], depth = I->c[2], fb = I->c[3], mix = I->c[4];
    double inc = rate / I->sr;
    for (unsigned i = 0; i < n; i++) {
        S->ph += inc; if (S->ph >= 1) S->ph -= 1;
        for (int c = 0; c < 2; c++) {
            float lfo = 0.5f + 0.5f * sinf(2.f * (float)M_PI * (float)(S->ph + c * 0.25));
            float f = 250.f * powf(2.f, lfo * depth * 4.f);
            float t = tanf((float)M_PI * fminf(f, I->sr * 0.45f) / I->sr), a = (1 - t) / (1 + t);
            float x = in[c][i] + S->last[c] * fb;
            for (int s = 0; s < PH_ST; s++) {
                float y = -a * x + S->z[c][s];
                S->z[c][s] = x + a * y;
                x = y;
            }
            S->last[c] = x;
            out[c][i] = wet(in[c][i], (in[c][i] + x) * 0.5f, mix);
        }
    }
}
static const char *const phaser_cn[] = {"Enabled", "Rate", "Depth", "Feedback", "Mix"};
static const float phaser_cr[][3] = {{0, 1, 0}, {0.02f, 5, 0.5f}, {0, 1, 0.7f}, {0, 0.95f, 0.4f}, {0, 1, 1}};

/* ---- tremolo */
typedef struct { double ph; } trem_t;
static void trem_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    trem_t *S = I->st;
    float depth = I->c[2], st = I->c[3] > 0.5f ? 0.5f : 0.f;
    double inc = I->c[1] / I->sr;
    for (unsigned i = 0; i < n; i++) {
        S->ph += inc; if (S->ph >= 1) S->ph -= 1;
        for (int c = 0; c < 2; c++) {
            float lfo = 0.5f + 0.5f * sinf(2.f * (float)M_PI * (float)(S->ph + c * st));
            out[c][i] = in[c][i] * (1.f - depth * lfo);
        }
    }
}
static const char *const trem_cn[] = {"Enabled", "Rate", "Depth", "Stereo"};
static const float trem_cr[][3] = {{0, 1, 0}, {0.5f, 20, 5}, {0, 1, 0.6f}, {0, 1, 0}};

/* ---- ring modulator */
typedef struct { double ph; } ring_t;
static void ring_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    ring_t *S = I->st;
    double inc = I->c[1] / I->sr;
    float mix = I->c[2];
    for (unsigned i = 0; i < n; i++) {
        S->ph += inc; if (S->ph >= 1) S->ph -= 1;
        float m = sinf(2.f * (float)M_PI * (float)S->ph);
        for (int c = 0; c < 2; c++) out[c][i] = wet(in[c][i], in[c][i] * m, mix);
    }
}
static const char *const ring_cn[] = {"Enabled", "Frequency", "Mix"};
static const float ring_cr[][3] = {{0, 1, 0}, {1, 2000, 440}, {0, 1, 1}};

/* ---- envelope filter (auto-wah): resonant SVF whose cutoff follows the input level */
typedef struct { float env, ic1[2], ic2[2]; } envf_t;
static void envf_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    envf_t *S = I->st;
    float sens = I->c[1], q = fmaxf(0.5f, I->c[2]), lo = I->c[3], oct = I->c[4], mix = I->c[5];
    float at = tc(4.f, I->sr), rt = tc(120.f, I->sr), k = 1.f / q;
    for (unsigned i = 0; i < n; i++) {
        float x = fmaxf(fabsf(in[0][i]), fabsf(in[1][i]));
        S->env = x + (S->env - x) * (x > S->env ? at : rt);
        float e = clampf(S->env * (1.f + sens * 40.f), 0.f, 1.f);
        float fc = fminf(lo * powf(2.f, oct * e), I->sr * 0.42f);
        float g = tanf((float)M_PI * fc / I->sr), a1 = 1 / (1 + g * (g + k)), a2 = g * a1, a3 = g * a2;
        for (int c = 0; c < 2; c++) {
            float v3 = in[c][i] - S->ic2[c];
            float v1 = a1 * S->ic1[c] + a2 * v3, v2 = S->ic2[c] + a2 * S->ic1[c] + a3 * v3;
            S->ic1[c] = 2 * v1 - S->ic1[c];
            S->ic2[c] = 2 * v2 - S->ic2[c];
            out[c][i] = wet(in[c][i], v2 + v1 * 0.5f, mix);
        }
    }
}
static const char *const envf_cn[] = {"Enabled", "Sensitivity", "Resonance", "Low Freq", "Range", "Mix"};
static const float envf_cr[][3] = {{0, 1, 0}, {0, 1, 0.5f}, {0.5f, 12, 4}, {80, 1000, 250}, {1, 6, 4}, {0, 1, 1}};

/* ---- pitch shifter (two crossfaded delay taps) */
typedef struct { float *b[2]; int n, w; float ph; } pitch_t;
#define PS_WIN 0.045f
static void pitch_init(inst_t *I) {
    pitch_t *S = I->st;
    S->n = (int)(PS_WIN * I->sr) * 2 + 8;
    S->b[0] = ibuf(I, S->n);
    S->b[1] = ibuf(I, S->n);
}
static void pitch_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    pitch_t *S = I->st;
    float r = powf(2.f, I->c[1] / 12.f), W = PS_WIN * I->sr, mix = I->c[2];
    float inc = (1.f - r) / W;
    for (unsigned i = 0; i < n; i++) {
        S->ph += inc;
        S->ph -= floorf(S->ph);
        float p2 = S->ph + 0.5f; p2 -= floorf(p2);
        float g1 = 0.5f - 0.5f * cosf(2.f * (float)M_PI * S->ph), g2 = 0.5f - 0.5f * cosf(2.f * (float)M_PI * p2);
        for (int c = 0; c < 2; c++) {
            S->b[c][S->w] = in[c][i];
            float y = rd_interp(S->b[c], S->n, S->w, 2 + S->ph * W) * g1 + rd_interp(S->b[c], S->n, S->w, 2 + p2 * W) * g2;
            out[c][i] = wet(in[c][i], y, mix);
        }
        S->w = (S->w + 1) % S->n;
    }
}
static const char *const pitch_cn[] = {"Enabled", "Semitones", "Mix"};
static const float pitch_cr[][3] = {{0, 1, 0}, {-12, 12, 0}, {0, 1, 1}};

/* ---- delay (damping, ping-pong) */
typedef struct { float *b[2]; int n, w; float lp[2]; } delay_t;
static void delay_init(inst_t *I) {
    delay_t *S = I->st;
    S->n = (int)(2.05f * I->sr);
    S->b[0] = ibuf(I, S->n);
    S->b[1] = ibuf(I, S->n);
}
static void delay_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    delay_t *S = I->st;
    float d = clampf(I->c[1] * 0.001f * I->sr, 1, S->n - 2), fb = I->c[2], damp = I->c[3] * 0.9f, mix = I->c[5];
    int pp = I->c[4] > 0.5f;
    for (unsigned i = 0; i < n; i++) {
        float y[2];
        for (int c = 0; c < 2; c++) {
            y[c] = rd_interp(S->b[c], S->n, S->w, d);
            S->lp[c] = y[c] * (1 - damp) + S->lp[c] * damp;
        }
        if (pp) {   /* mono input into the left line, the lines feed each other */
            S->b[0][S->w] = (in[0][i] + in[1][i]) * 0.5f + S->lp[1] * fb;
            S->b[1][S->w] = S->lp[0] * fb;
        } else
            for (int c = 0; c < 2; c++) S->b[c][S->w] = in[c][i] + S->lp[c] * fb;
        for (int c = 0; c < 2; c++) out[c][i] = in[c][i] + y[c] * mix;
        S->w = (S->w + 1) % S->n;
    }
}
static const char *const delay_cn[] = {"Enabled", "Time", "Feedback", "Damping", "Ping-Pong", "Mix"};
static const float delay_cr[][3] = {{0, 1, 0}, {1, 2000, 350}, {0, 0.95f, 0.4f}, {0, 1, 0.3f}, {0, 1, 0}, {0, 1, 0.35f}};

/* ---- reverse delay: two windowed readers walk backwards through the last 2T of audio */
typedef struct { float *b[2]; int n, w; float ph; } rdel_t;
static void rdel_init(inst_t *I) {
    rdel_t *S = I->st;
    S->n = (int)(4.1f * I->sr);
    S->b[0] = ibuf(I, S->n);
    S->b[1] = ibuf(I, S->n);
}
static void rdel_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    rdel_t *S = I->st;
    float T = clampf(I->c[1] * 0.001f * I->sr, 64, S->n / 2 - 4), fb = I->c[2], mix = I->c[3];
    for (unsigned i = 0; i < n; i++) {
        S->ph += 1.f / T; S->ph -= floorf(S->ph);
        float p2 = S->ph + 0.5f; p2 -= floorf(p2);
        float g1 = sinf((float)M_PI * S->ph), g2 = sinf((float)M_PI * p2);
        g1 *= g1; g2 *= g2;
        for (int c = 0; c < 2; c++) {
            float y = rd_interp(S->b[c], S->n, S->w, 1 + 2 * S->ph * T) * g1 + rd_interp(S->b[c], S->n, S->w, 1 + 2 * p2 * T) * g2;
            S->b[c][S->w] = in[c][i] + y * fb;
            out[c][i] = in[c][i] + y * mix;
        }
        S->w = (S->w + 1) % S->n;
    }
}
static const char *const rdel_cn[] = {"Enabled", "Time", "Feedback", "Mix"};
static const float rdel_cr[][3] = {{0, 1, 0}, {50, 2000, 500}, {0, 0.9f, 0.3f}, {0, 1, 0.5f}};

/* ---- reverb (Freeverb topology + predelay) */
#define RV_C 8
#define RV_A 4
static const int rv_ct[RV_C] = {1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617};
static const int rv_at[RV_A] = {556, 441, 341, 225};
typedef struct {
    float *cb[2][RV_C], *ab[2][RV_A], *pd[2], cf[2][RV_C];
    int cn[2][RV_C], an[2][RV_A], ci[2][RV_C], ai[2][RV_A], pn, pw;
} rv_t;
static void rv_init(inst_t *I) {
    rv_t *S = I->st;
    float k = I->sr / 44100.f;
    for (int c = 0; c < 2; c++) {
        for (int j = 0; j < RV_C; j++) { S->cn[c][j] = (int)((rv_ct[j] + c * 23) * k); S->cb[c][j] = ibuf(I, S->cn[c][j]); }
        for (int j = 0; j < RV_A; j++) { S->an[c][j] = (int)((rv_at[j] + c * 23) * k); S->ab[c][j] = ibuf(I, S->an[c][j]); }
    }
    S->pn = (int)(0.21f * I->sr);
    S->pd[0] = ibuf(I, S->pn);
    S->pd[1] = ibuf(I, S->pn);
}
static void rv_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    rv_t *S = I->st;
    float room = 0.7f + I->c[1] * 0.28f, damp = I->c[2] * 0.4f, mix = I->c[4];
    int pre = (int)clampf(I->c[3] * 0.001f * I->sr, 0, S->pn - 1);
    for (unsigned i = 0; i < n; i++) {
        float x[2], y[2];
        for (int c = 0; c < 2; c++) {
            S->pd[c][S->pw] = in[c][i];
            int r = S->pw - pre; if (r < 0) r += S->pn;
            x[c] = S->pd[c][r];
        }
        S->pw = (S->pw + 1) % S->pn;
        float m = (x[0] + x[1]) * 0.015f;
        for (int c = 0; c < 2; c++) {
            float acc = 0;
            for (int j = 0; j < RV_C; j++) {
                float *b = S->cb[c][j], o = b[S->ci[c][j]];
                S->cf[c][j] = o * (1 - damp) + S->cf[c][j] * damp;
                b[S->ci[c][j]] = m + S->cf[c][j] * room;
                if (++S->ci[c][j] >= S->cn[c][j]) S->ci[c][j] = 0;
                acc += o;
            }
            for (int j = 0; j < RV_A; j++) {
                float *b = S->ab[c][j], o = b[S->ai[c][j]];
                b[S->ai[c][j]] = acc + o * 0.5f;
                acc = o - acc;
                if (++S->ai[c][j] >= S->an[c][j]) S->ai[c][j] = 0;
            }
            y[c] = acc;
        }
        for (int c = 0; c < 2; c++) out[c][i] = in[c][i] * (1 - mix * 0.5f) + y[c] * mix * 2.f;
    }
}
static const char *const rv_cn[] = {"Enabled", "Size", "Damping", "Predelay", "Mix"};
static const float rv_cr[][3] = {{0, 1, 0}, {0, 1, 0.6f}, {0, 1, 0.4f}, {0, 200, 20}, {0, 1, 0.3f}};

/* ================================================================== EasyEffects-style modules */
/* ---- shared: Linkwitz-Riley 3-band crossover (LR4, the low band is all-pass corrected) */
typedef struct { bq_t l1[2], h1[2], l2[2], h2[2], ap; float f1, f2; } xo3_t;
static void xo3_set(xo3_t *X, float sr, float f1, float f2) {
    if (f2 < f1 * 1.5f) f2 = f1 * 1.5f;
    if (X->f1 == f1 && X->f2 == f2) return;
    X->f1 = f1; X->f2 = f2;
    for (int i = 0; i < 2; i++) {
        bq_set(&X->l1[i], BQ_LP, sr, f1, 0.7071f, 0); bq_set(&X->h1[i], BQ_HP, sr, f1, 0.7071f, 0);
        bq_set(&X->l2[i], BQ_LP, sr, f2, 0.7071f, 0); bq_set(&X->h2[i], BQ_HP, sr, f2, 0.7071f, 0);
    }
    bq_set(&X->ap, BQ_AP, sr, f2, 0.7071f, 0);
}
static inline void xo3_run(xo3_t *X, int c, float x, float b[3]) {
    float lo = bq_run(&X->l1[1], c, bq_run(&X->l1[0], c, x));
    float r = bq_run(&X->h1[1], c, bq_run(&X->h1[0], c, x));
    b[0] = bq_run(&X->ap, c, lo);
    b[1] = bq_run(&X->l2[1], c, bq_run(&X->l2[0], c, r));
    b[2] = bq_run(&X->h2[1], c, bq_run(&X->h2[0], c, r));
}

/* ---- auto gain: K-weighted loudness, slowly steered towards a target (silence is ignored) */
typedef struct { bq_t kh, ks; float fast, slow, g; int primed; } agc_t;
static void agc_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    agc_t *S = I->st;
    bq_set(&S->kh, BQ_HP, I->sr, 38.f, 0.5f, 0);
    bq_set(&S->ks, BQ_HIGHSHELF, I->sr, 1681.f, 0.7071f, 4.f);
    float tgt = I->c[1], maxg = I->c[2], maxc = I->c[3], spd = fmaxf(0.2f, I->c[4]);
    float kf = tc(400.f, I->sr), ks = tc(spd * 1000.f, I->sr), kg = tc(spd * 500.f, I->sr);
    if (!S->primed) { S->slow = db2lin(tgt) * db2lin(tgt); S->g = 0; S->primed = 1; }
    for (unsigned i = 0; i < n; i++) {
        float ms = 0;
        for (int c = 0; c < 2; c++) { float k = bq_run(&S->ks, c, bq_run(&S->kh, c, in[c][i])); ms += k * k; }
        ms *= 0.5f;
        S->fast = ms + (S->fast - ms) * kf;
        if (S->fast > 1e-6f) S->slow = ms + (S->slow - ms) * ks;     /* gate: below ~-60 LUFS, hold */
        float lvl = 10.f * log10f(S->slow + 1e-12f) - 0.691f;
        float want = clampf(tgt - lvl, -maxc, maxg);
        S->g = want + (S->g - want) * kg;
        float g = db2lin(S->g);
        out[0][i] = in[0][i] * g;
        out[1][i] = in[1][i] * g;
    }
}
static const char *const agc_cn[] = {"Enabled", "Target", "Max Gain", "Max Cut", "Speed"};
static const float agc_cr[][3] = {{0, 1, 0}, {-36, -6, -18}, {0, 30, 12}, {0, 30, 12}, {0.5f, 20, 3}};

/* ---- harmonic generators: bass enhancer and exciter */
typedef struct { bq_t band[2], strip[2], dc; } harm_t;
static inline float lo_sat(float x, float drv) { return x - tanhf(x * drv) / drv; }
static void bassenh_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    harm_t *S = I->st;
    float amt = I->c[1], f = I->c[2], drv = 1.f + I->c[3] * 3.f, fl = I->c[4];
    for (int k = 0; k < 2; k++) { bq_set(&S->band[k], BQ_LP, I->sr, f, 0.7071f, 0);
                                  bq_set(&S->strip[k], BQ_HP, I->sr, f, 0.7071f, 0); }
    bq_set(&S->dc, BQ_HP, I->sr, 15.f, 0.7071f, 0);
    for (unsigned i = 0; i < n; i++)
        for (int c = 0; c < 2; c++) {
            float lo = bq_run(&S->band[1], c, bq_run(&S->band[0], c, in[c][i]));
            /* only what the saturation adds (odd + even harmonics), never the clean signal */
            float h = lo_sat(lo, drv) + 0.5f * drv * lo * lo;
            h = bq_run(&S->dc, c, h);
            float gen = fl > 0.5f ? h : bq_run(&S->strip[1], c, bq_run(&S->strip[0], c, h));
            out[c][i] = in[c][i] + gen * amt * 4.f;
        }
}
static const char *const bassenh_cn[] = {"Enabled", "Amount", "Frequency", "Harmonics", "Include Fundamental"};
static const float bassenh_cr[][3] = {{0, 1, 0}, {0, 1, 0.4f}, {40, 250, 100}, {0, 10, 5}, {0, 1, 0}};

static void exciter_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    harm_t *S = I->st;
    float amt = I->c[1], f = I->c[2], drv = 1.f + I->c[3] * 3.f;
    for (int k = 0; k < 2; k++) { bq_set(&S->band[k], BQ_HP, I->sr, f, 0.7071f, 0);
                                  bq_set(&S->strip[k], BQ_HP, I->sr, f, 0.7071f, 0); }
    for (unsigned i = 0; i < n; i++)
        for (int c = 0; c < 2; c++) {
            float hi = bq_run(&S->band[1], c, bq_run(&S->band[0], c, in[c][i]));
            float h = (lo_sat(hi, drv)) + 0.5f * drv * hi * hi;       /* odd + even harmonics only */
            h = bq_run(&S->strip[1], c, bq_run(&S->strip[0], c, h));
            out[c][i] = in[c][i] + h * amt * 4.f;
        }
}
static const char *const exciter_cn[] = {"Enabled", "Amount", "Frequency", "Harmonics"};
static const float exciter_cr[][3] = {{0, 1, 0}, {0, 1, 0.3f}, {1000, 12000, 4000}, {0, 10, 5}};

/* ---- bass loudness: low + high shelves, like listening louder */
typedef struct { bq_t lo, hi; } bloud_t;
static void bloud_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    bloud_t *S = I->st;
    float a = I->c[1];
    bq_set(&S->lo, BQ_LOWSHELF, I->sr, I->c[2], 0.6f, a);
    bq_set(&S->hi, BQ_HIGHSHELF, I->sr, 9000.f, 0.6f, a * I->c[3]);
    float g = db2lin(I->c[4]);
    for (unsigned i = 0; i < n; i++)
        for (int c = 0; c < 2; c++) out[c][i] = bq_run(&S->hi, c, bq_run(&S->lo, c, in[c][i])) * g;
}
static const char *const bloud_cn[] = {"Enabled", "Amount", "Bass Frequency", "Treble", "Output"};
static const float bloud_cr[][3] = {{0, 1, 0}, {0, 18, 6}, {40, 250, 100}, {0, 1, 0.35f}, {-24, 6, -3}};

/* ---- crystalizer: emphasise sample-to-sample change (sharpens transients) */
typedef struct { float prev[2]; } cryst_t;
static void cryst_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    cryst_t *S = I->st;
    float k = I->c[1], g = db2lin(I->c[2]);
    for (unsigned i = 0; i < n; i++)
        for (int c = 0; c < 2; c++) {
            float x = in[c][i];
            out[c][i] = (x + k * (x - S->prev[c])) * g;
            S->prev[c] = x;
        }
}
static const char *const cryst_cn[] = {"Enabled", "Intensity", "Output"};
static const float cryst_cr[][3] = {{0, 1, 0}, {0, 10, 2}, {-24, 6, -3}};

/* ---- downward expander */
typedef struct { float env, gr; } expd_t;
static void expd_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    expd_t *S = I->st;
    float thr = I->c[1], ratio = fmaxf(1.f, I->c[2]), maxr = I->c[3];
    float at = tc(I->c[4], I->sr), rt = tc(I->c[5], I->sr), er = tc(20.f, I->sr);
    for (unsigned i = 0; i < n; i++) {
        float pk = fmaxf(fabsf(in[0][i]), fabsf(in[1][i]));
        S->env = pk > S->env ? pk : pk + (S->env - pk) * er;
        float d = thr - lin2db(S->env);
        float want = d > 0 ? fminf(d * (ratio - 1.f), maxr) : 0.f;
        S->gr = want + (S->gr - want) * (want > S->gr ? rt : at);   /* reducing = closing = release */
        float g = db2lin(-S->gr);
        out[0][i] = in[0][i] * g;
        out[1][i] = in[1][i] * g;
    }
}
static const char *const expd_cn[] = {"Enabled", "Threshold", "Ratio", "Max Reduction", "Attack", "Release"};
static const float expd_cr[][3] = {{0, 1, 0}, {-80, 0, -40}, {1, 10, 2}, {0, 90, 40}, {0.1f, 100, 5}, {10, 2000, 200}};

/* ---- filter: low / high / band pass, notch, 12-48 dB per octave */
typedef struct { bq_t b[4]; } filt_t;
static const int filt_map[] = {BQ_LP, BQ_HP, BQ_BP, BQ_NOTCH};
static void filt_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    filt_t *S = I->st;
    int t = (int)clampf(I->c[1], 0, 3), st = 1 << (int)clampf(I->c[4], 0, 2);
    for (int k = 0; k < st; k++) bq_set(&S->b[k], filt_map[t], I->sr, I->c[2], (t >= 2 || k == st - 1) ? I->c[3] : 0.7071f, 0);
    for (unsigned i = 0; i < n; i++)
        for (int c = 0; c < 2; c++) {
            float x = in[c][i];
            for (int k = 0; k < st; k++) x = bq_run(&S->b[k], c, x);
            out[c][i] = x;
        }
}
static const char *const filt_cn[] = {"Enabled", "Type", "Frequency", "Resonance", "Slope"};
static const float filt_cr[][3] = {{0, 1, 0}, {0, 3, 0}, {20, 20000, 1000}, {0.1f, 10, 0.7071f}, {0, 2, 0}};

/* ---- maximizer: push the level into a lookahead limiter */
static void maxi_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    /* reuse the limiter: its controls are [Enabled, Ceiling, Release]; ours [Enabled, Threshold, Ceiling, Release] */
    float save[3] = {I->c[1], I->c[2], I->c[3]}, g = db2lin(-I->c[1]);
    static __thread float pre[2][CHUNK];
    for (unsigned i = 0; i < n; i++) { pre[0][i] = in[0][i] * g; pre[1][i] = in[1][i] * g; }
    I->c[1] = save[1]; I->c[2] = save[2];
    float *const p[2] = {pre[0], pre[1]};
    lim_run(I, p, out, n);
    I->c[1] = save[0]; I->c[2] = save[1]; I->c[3] = save[2];
}
static const char *const maxi_cn[] = {"Enabled", "Threshold", "Ceiling", "Release"};
static const float maxi_cr[][3] = {{0, 1, 0}, {-30, 0, -6}, {-12, 0, -0.5f}, {1, 1000, 60}};

/* ---- multiband compressor (3 bands) */
typedef struct { xo3_t x; float gr[3]; } mbc_t;
static void mbc_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    mbc_t *S = I->st;
    xo3_set(&S->x, I->sr, I->c[1], I->c[2]);
    float at = tc(I->c[9], I->sr), rt = tc(I->c[10], I->sr), mk = db2lin(I->c[11]), g[3];
    for (unsigned i = 0; i < n; i++) {
        float b[2][3];
        xo3_run(&S->x, 0, in[0][i], b[0]);
        xo3_run(&S->x, 1, in[1][i], b[1]);
        for (int k = 0; k < 3; k++) {
            float thr = I->c[3 + 2 * k], ratio = fmaxf(1.f, I->c[4 + 2 * k]);
            float d = lin2db(fmaxf(fabsf(b[0][k]), fabsf(b[1][k]))) - thr;
            float want = d > 0 ? d * (1.f - 1.f / ratio) : 0.f;
            S->gr[k] = want + (S->gr[k] - want) * (want > S->gr[k] ? at : rt);
            g[k] = db2lin(-S->gr[k]);
        }
        for (int c = 0; c < 2; c++) out[c][i] = (b[c][0] * g[0] + b[c][1] * g[1] + b[c][2] * g[2]) * mk;
    }
}
static const char *const mbc_cn[] = {"Enabled", "Low Crossover", "High Crossover", "Low Threshold", "Low Ratio",
                                     "Mid Threshold", "Mid Ratio", "High Threshold", "High Ratio", "Attack", "Release", "Makeup"};
static const float mbc_cr[][3] = {{0, 1, 0}, {40, 1000, 200}, {1000, 12000, 3000}, {-60, 0, -24}, {1, 20, 3},
                                  {-60, 0, -24}, {1, 20, 3}, {-60, 0, -24}, {1, 20, 3}, {0.1f, 200, 10}, {10, 2000, 150}, {-12, 24, 0}};

/* ---- multiband gate (3 bands) */
typedef struct { xo3_t x; float env[3], g[3]; int hold[3]; } mbg_t;
static void mbg_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    mbg_t *S = I->st;
    xo3_set(&S->x, I->sr, I->c[1], I->c[2]);
    float range = db2lin(I->c[6]), at = tc(I->c[7], I->sr), rt = tc(I->c[9], I->sr), er = tc(10.f, I->sr);
    int hold = (int)(I->c[8] * 0.001f * I->sr);
    for (unsigned i = 0; i < n; i++) {
        float b[2][3];
        xo3_run(&S->x, 0, in[0][i], b[0]);
        xo3_run(&S->x, 1, in[1][i], b[1]);
        for (int k = 0; k < 3; k++) {
            float pk = fmaxf(fabsf(b[0][k]), fabsf(b[1][k]));
            S->env[k] = pk > S->env[k] ? pk : pk + (S->env[k] - pk) * er;
            if (S->env[k] > db2lin(I->c[3 + k])) S->hold[k] = hold;
            float want = S->hold[k] > 0 ? 1.f : range;
            if (S->hold[k] > 0) S->hold[k]--;
            S->g[k] = want + (S->g[k] - want) * (want > S->g[k] ? at : rt);
        }
        for (int c = 0; c < 2; c++) out[c][i] = b[c][0] * S->g[0] + b[c][1] * S->g[1] + b[c][2] * S->g[2];
    }
}
static const char *const mbg_cn[] = {"Enabled", "Low Crossover", "High Crossover", "Low Threshold", "Mid Threshold",
                                     "High Threshold", "Range", "Attack", "Hold", "Release"};
static const float mbg_cr[][3] = {{0, 1, 0}, {40, 1000, 200}, {1000, 12000, 3000}, {-90, 0, -50}, {-90, 0, -50},
                                  {-90, 0, -50}, {-90, 0, -40}, {0.1f, 100, 2}, {0, 1000, 100}, {5, 2000, 150}};

/* ---- stereo tools */
typedef struct { int dummy; } stt_t;
static void stt_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    float bal = I->c[1], width = I->c[2], gl = bal > 0 ? 1.f - bal : 1.f, gr = bal < 0 ? 1.f + bal : 1.f;
    int mono = I->c[3] > 0.5f, swap = I->c[4] > 0.5f;
    float il = I->c[5] > 0.5f ? -1.f : 1.f, ir = I->c[6] > 0.5f ? -1.f : 1.f;
    for (unsigned i = 0; i < n; i++) {
        float l = in[swap][i] * il, r = in[!swap][i] * ir;
        float m = (l + r) * 0.5f, s = mono ? 0.f : (l - r) * 0.5f * width;
        out[0][i] = (m + s) * gl;
        out[1][i] = (m - s) * gr;
    }
}
static const char *const stt_cn[] = {"Enabled", "Balance", "Width", "Mono", "Swap", "Invert L", "Invert R"};
static const float stt_cr[][3] = {{0, 1, 0}, {-1, 1, 0}, {0, 2, 1}, {0, 1, 0}, {0, 1, 0}, {0, 1, 0}, {0, 1, 0}};

/* ---- crossfeed (bs2b style): low-passed opposite channel, flat for mono content */
typedef struct { float lp[2]; bq_t hs; } xf_t;
static void xf_run(inst_t *I, float *const in[2], float *out[2], unsigned n) {
    xf_t *S = I->st;
    float g = db2lin(-I->c[2]), a = expf(-2.f * (float)M_PI * I->c[1] / I->sr), norm = 1.f / (1.f + g);
    bq_set(&S->hs, BQ_HIGHSHELF, I->sr, I->c[1], 0.5f, lin2db(1.f + g));
    for (unsigned i = 0; i < n; i++) {
        float x[2] = {in[0][i], in[1][i]};
        for (int c = 0; c < 2; c++) S->lp[c] = x[c] * (1.f - a) + S->lp[c] * a;
        for (int c = 0; c < 2; c++) out[c][i] = (bq_run(&S->hs, c, x[c]) + g * S->lp[!c]) * norm;
    }
}
static const char *const xf_cn[] = {"Enabled", "Cutoff", "Feed"};
static const float xf_cr[][3] = {{0, 1, 0}, {300, 2000, 700}, {1, 15, 4.5f}};

/* ================================================================== registry */
#define FX(lbl, nm, pfx, st, ini, fin) \
    {lbl, nm, (int)(sizeof pfx##_cn / sizeof *pfx##_cn), pfx##_cn, pfx##_cr, sizeof(st), ini, pfx##_run, fin}
static const fxdef_t FXS[] = {
    FX("amx_denoise", "AMX Noise Suppression", denoise, denoise_t, denoise_init, denoise_fini),
    FX("amx_gate", "AMX Noise Gate", gate, gate_t, NULL, NULL),
    FX("amx_lowcut", "AMX Low Cut", lowcut, lowcut_t, NULL, NULL),
    FX("amx_eq3", "AMX Tone EQ", eq3, eq3_t, NULL, NULL),
    FX("amx_peq", "AMX Parametric EQ", peq, peq_t, peq_init, NULL),
    FX("amx_deess", "AMX De-esser", deess, deess_t, NULL, NULL),
    FX("amx_comp", "AMX Compressor", comp, comp_t, NULL, NULL),
    FX("amx_limiter", "AMX Limiter", lim, lim_t, lim_init, lim_fini),
    FX("amx_loud", "AMX Loudness Limiter", loud, loud_t, loud_init, NULL),
    FX("amx_ceq", "AMX Creative EQ", ceq, ceq_t, NULL, NULL),
    FX("amx_overdrive", "AMX Overdrive", od, od_t, NULL, NULL),
    FX("amx_tape", "AMX Tape Saturator", tape, tape_t, NULL, NULL),
    FX("amx_crush", "AMX Bitcrusher", crush, crush_t, NULL, NULL),
    FX("amx_chorus", "AMX Chorus", chorus, mdl_t, mdl_init, NULL),
    FX("amx_flanger", "AMX Flanger", flanger, mdl_t, mdl_init, NULL),
    FX("amx_phaser", "AMX Phaser", phaser, phaser_t, NULL, NULL),
    FX("amx_tremolo", "AMX Tremolo", trem, trem_t, NULL, NULL),
    FX("amx_ring", "AMX Ring Modulator", ring, ring_t, NULL, NULL),
    FX("amx_envfilter", "AMX Envelope Filter", envf, envf_t, NULL, NULL),
    FX("amx_pitch", "AMX Pitch Shifter", pitch, pitch_t, pitch_init, NULL),
    FX("amx_delay", "AMX Delay", delay, delay_t, delay_init, NULL),
    FX("amx_revdelay", "AMX Reverse Delay", rdel, rdel_t, rdel_init, NULL),
    FX("amx_reverb", "AMX Reverb", rv, rv_t, rv_init, NULL),
    FX("amx_autogain", "AMX Auto Gain", agc, agc_t, NULL, NULL),
    FX("amx_bassenh", "AMX Bass Enhancer", bassenh, harm_t, NULL, NULL),
    FX("amx_exciter", "AMX Exciter", exciter, harm_t, NULL, NULL),
    FX("amx_bassloud", "AMX Bass Loudness", bloud, bloud_t, NULL, NULL),
    FX("amx_crystal", "AMX Crystalizer", cryst, cryst_t, NULL, NULL),
    FX("amx_expander", "AMX Expander", expd, expd_t, NULL, NULL),
    FX("amx_filter", "AMX Filter", filt, filt_t, NULL, NULL),
    FX("amx_maximizer", "AMX Maximizer", maxi, lim_t, lim_init, lim_fini),
    FX("amx_mbcomp", "AMX Multiband Compressor", mbc, mbc_t, NULL, NULL),
    FX("amx_mbgate", "AMX Multiband Gate", mbg, mbg_t, NULL, NULL),
    FX("amx_stereo", "AMX Stereo Tools", stt, stt_t, NULL, NULL),
    FX("amx_crossfeed", "AMX Crossfeed", xf, xf_t, NULL, NULL),
};
#define NFX ((int)(sizeof FXS / sizeof *FXS))

static LADSPA_Descriptor DESC[NFX];
static LADSPA_PortDescriptor PD[NFX][4 + MAXC];
static const char *PN[NFX][4 + MAXC];
static LADSPA_PortRangeHint PH[NFX][4 + MAXC];
static int ready;

static void build(void) {
    for (int k = 0; k < NFX; k++) {
        const fxdef_t *f = &FXS[k];
        static const char *an[4] = {"In L", "In R", "Out L", "Out R"};
        for (int p = 0; p < 4; p++) {
            PD[k][p] = (p < 2 ? P_IN : P_OUT) | P_AUDIO;
            PN[k][p] = an[p];
            PH[k][p].HintDescriptor = 0;
        }
        for (int i = 0; i < f->nctl; i++) {
            PD[k][4 + i] = P_IN | P_CTL;
            PN[k][4 + i] = f->cn[i];
            PH[k][4 + i].HintDescriptor = H_BELOW | H_ABOVE | (i == 0 ? H_TOGGLED : 0);
            PH[k][4 + i].LowerBound = f->cr[i][0];
            PH[k][4 + i].UpperBound = f->cr[i][1];
        }
        LADSPA_Descriptor *d = &DESC[k];
        d->UniqueID = 0x4D5800 + k;
        d->Label = f->label;
        d->Properties = 4; /* HARD_RT_CAPABLE */
        d->Name = f->name;
        d->Maker = "Audio Mixer";
        d->Copyright = "MIT";
        d->PortCount = 4 + f->nctl;
        d->PortDescriptors = PD[k];
        d->PortNames = PN[k];
        d->PortRangeHints = PH[k];
        d->ImplementationData = (void *)f;
        d->instantiate = fx_instantiate;
        d->connect_port = fx_connect;
        d->run = fx_run;
        d->cleanup = fx_cleanup;
    }
    ready = 1;
}

const LADSPA_Descriptor *ladspa_descriptor(unsigned long i) {
    if (!ready) build();
    return i < (unsigned long)NFX ? &DESC[i] : NULL;
}
