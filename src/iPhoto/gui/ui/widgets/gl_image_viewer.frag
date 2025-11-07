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
uniform vec4  uBWParams;
uniform float uTime;
uniform vec2  uViewSize;
uniform vec2  uTexSize;
uniform float uScale;
uniform vec2  uPan;

float clamp01(float x) { return clamp(x, 0.0, 1.0); }

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

float rand(vec2 n) {
    return fract(sin(dot(n, vec2(12.9898, 4.1414))) * 43758.5453);
}

vec3 apply_bw(vec3 color, vec2 uv) {
    float intensity = clamp(uBWParams.x, 0.0, 1.0);
    float neutrals = clamp(uBWParams.y, -1.0, 1.0);
    float tone = clamp(uBWParams.z, -1.0, 1.0);
    float grain = clamp(uBWParams.w, 0.0, 1.0);

    if (intensity <= 0.0 && grain <= 0.0) {
        return color;
    }

    const vec3 C_LUMA = vec3(0.2126, 0.7152, 0.0722);
    float luma = dot(color, C_LUMA);
    vec3 bw = mix(color, vec3(luma), intensity);

    if (abs(neutrals) > 1e-4) {
        vec3 neutral_mix = mix(bw, color, abs(neutrals));
        if (neutrals > 0.0) {
            bw = neutral_mix;
        } else {
            // Negative neutrals deepen the monochrome mix while preserving highlights.
            bw = mix(neutral_mix, bw, 0.5);
        }
    }

    if (abs(tone) > 1e-4) {
        float centred = luma * 2.0 - 1.0;
        float tone_mix = (centred + tone * (1.0 - abs(centred))) * 0.5 + 0.5;
        bw = mix(bw, vec3(tone_mix), abs(tone));
    }

    if (grain > 0.0) {
        vec2 grain_seed = uv + vec2(uTime, uTime * 0.37);
        float noise = rand(grain_seed) * 2.0 - 1.0;
        bw = mix(bw, clamp(bw * (1.0 + noise * 0.2), 0.0, 1.0), grain);
    }

    return bw;
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
