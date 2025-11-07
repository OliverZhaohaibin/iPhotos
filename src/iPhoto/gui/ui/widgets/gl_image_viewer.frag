#version 330 core
in vec2 vUV;
out vec4 FragColor;

uniform sampler2D uTex;

uniform float uBrilliance;
uniform float uExposure;
uniform float uHighlights;
uniform float uShadows;
uniform float uBrightness;
uniform float uContrast;
uniform float uBlackPoint;
uniform float uSaturation;
uniform float uVibrance;
uniform float uColorCast;
uniform vec3  uGain;
uniform float uIntensity01;
uniform float uNeutrals;
uniform float uTone;
uniform float uGrain;
uniform float uTime;
uniform vec2  uViewSize;
uniform vec2  uTexSize;
uniform float uScale;
uniform vec2  uPan;

float clamp01(float x) { return clamp(x, 0.0, 1.0); }

float luminance(vec3 color) {
    // Use Rec. 709 coefficients to match the CPU preview pipeline.
    return dot(color, vec3(0.2126, 0.7152, 0.0722));
}

float apply_channel(float value,
                    float exposure,
                    float brightness,
                    float brilliance,
                    float highlights,
                    float shadows,
                    float contrast_factor,
                    float black_point)
{
    float adjusted = value + exposure + brightness;
    float mid_distance = value - 0.5;
    adjusted += brilliance * (1.0 - pow(mid_distance * 2.0, 2.0));

    if (adjusted > 0.65) {
        float ratio = (adjusted - 0.65) / 0.35;
        adjusted += highlights * ratio;
    } else if (adjusted < 0.35) {
        float ratio = (0.35 - adjusted) / 0.35;
        adjusted += shadows * ratio;
    }

    adjusted = (adjusted - 0.5) * contrast_factor + 0.5;

    if (black_point > 0.0)
        adjusted -= black_point * (1.0 - adjusted);
    else if (black_point < 0.0)
        adjusted -= black_point * adjusted;

    return clamp01(adjusted);
}

vec3 apply_color_transform(vec3 rgb,
                           float saturation,
                           float vibrance,
                           float colorCast,
                           vec3 gain)
{
    vec3 mixGain = (1.0 - colorCast) + gain * colorCast;
    rgb *= mixGain;

    float luma = dot(rgb, vec3(0.299, 0.587, 0.114));
    vec3  chroma = rgb - vec3(luma);
    float sat_amt = 1.0 + saturation;
    float vib_amt = 1.0 + vibrance;
    float w = 1.0 - clamp(abs(luma - 0.5) * 2.0, 0.0, 1.0);
    float chroma_scale = sat_amt * (1.0 + (vib_amt - 1.0) * w);
    chroma *= chroma_scale;
    return clamp(vec3(luma) + chroma, 0.0, 1.0);
}

float gamma_neutral(float gray, float neutral_amount) {
    // Neutral adjustments are centred around 0.5 so 0.5 is the no-op point.
    float n = 0.6 * (neutral_amount - 0.5);
    float gamma = pow(2.0, -n * 2.0);
    return pow(clamp(gray, 0.0, 1.0), gamma);
}

float contrast_tone(float gray, float tone_amount) {
    // Tone adjustments also centre around 0.5 and apply an S-curve.
    float t = tone_amount - 0.5;
    float k = (t >= 0.0) ? mix(1.0, 2.2, t * 2.0) : mix(1.0, 0.6, -t * 2.0);
    float x = clamp(gray, 0.0, 1.0);
    float eps = 1e-6;
    float x_safe = clamp(x, eps, 1.0 - eps);
    float logit = log(x_safe / clamp(1.0 - x_safe, eps, 1.0 - eps));
    float y = 1.0 / (1.0 + exp(-logit * k));
    return clamp(y, 0.0, 1.0);
}

float grain_noise(vec2 uv, float grain_amount) {
    // Match the CPU preview noise so thumbnails and the live view stay consistent.
    if (grain_amount <= 0.0) {
        return 0.0;
    }
    float noise = fract(sin(dot(uv, vec2(12.9898, 78.233))) * 43758.5453);
    return (noise - 0.5) * 0.2 * grain_amount;
}

vec3 apply_bw(vec3 color, vec2 uv) {
    float intensity = clamp(uIntensity01, 0.0, 1.0);
    float neutrals = clamp(uNeutrals, 0.0, 1.0);
    float tone = clamp(uTone, 0.0, 1.0);
    float grain = clamp(uGrain, 0.0, 1.0);

    if (abs(intensity - 0.5) <= 1e-4 && neutrals <= 1e-4 && tone <= 1e-4 && grain <= 0.0) {
        return color;
    }

    float g0 = luminance(color);

    float g_neutral = g0;
    float g_soft_base = pow(g0, 0.82);
    float g_soft = (contrast_tone(g_soft_base, 0.0) + g_soft_base) * 0.5;
    float g_rich = contrast_tone(pow(g0, 1.0 / 1.22), 0.35);

    float gray;
    if (intensity >= 0.5) {
        float t = (intensity - 0.5) / 0.5;
        gray = mix(g_neutral, g_rich, t);
    } else {
        float t = (0.5 - intensity) / 0.5;
        gray = mix(g_soft, g_neutral, t);
    }

    gray = gamma_neutral(gray, neutrals);
    gray = contrast_tone(gray, tone);
    gray += grain_noise(uv * uTexSize, grain);
    gray = clamp(gray, 0.0, 1.0);

    return vec3(gray);
}

void main() {
    if (uScale <= 0.0) {
        discard;
    }

    vec2 fragPx = vec2(gl_FragCoord.x - 0.5, gl_FragCoord.y - 0.5);
    vec2 viewCentre = uViewSize * 0.5;
    vec2 viewVector = fragPx - viewCentre;
    vec2 texVector = (viewVector - uPan) / uScale;
    vec2 texPx = texVector + (uTexSize * 0.5);
    vec2 uv = texPx / uTexSize;

    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
        discard;
    }

    uv.y = 1.0 - uv.y;

    vec4 texel = texture(uTex, uv);
    vec3 c = texel.rgb;

    float exposure_term    = uExposure   * 1.5;
    float brightness_term  = uBrightness * 0.75;
    float brilliance_term  = uBrilliance * 0.6;
    float contrast_factor  = 1.0 + uContrast;

    c.r = apply_channel(c.r, exposure_term, brightness_term, brilliance_term,
                        uHighlights, uShadows, contrast_factor, uBlackPoint);
    c.g = apply_channel(c.g, exposure_term, brightness_term, brilliance_term,
                        uHighlights, uShadows, contrast_factor, uBlackPoint);
    c.b = apply_channel(c.b, exposure_term, brightness_term, brilliance_term,
                        uHighlights, uShadows, contrast_factor, uBlackPoint);

    c = apply_color_transform(c, uSaturation, uVibrance, uColorCast, uGain);
    c = apply_bw(c, uv);
    FragColor = vec4(clamp(c, 0.0, 1.0), 1.0);
}
