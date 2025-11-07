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
    FragColor = vec4(clamp(c, 0.0, 1.0), 1.0);
}
