"""GLSL shader code for the GPU-accelerated image viewer."""

VERTEX_SHADER = """
#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec2 aTexCoord;
out vec2 TexCoord;
uniform float u_zoom;
uniform vec2 u_pan;
void main()
{
    gl_Position = vec4(aPos * u_zoom + vec3(u_pan, 0.0), 1.0);
    TexCoord = aTexCoord;
}
"""

FRAGMENT_SHADER = """
#version 330 core
out vec4 FragColor;
in vec2 TexCoord;
uniform sampler2D ourTexture;
uniform bool is_placeholder;
uniform float Brilliance;
uniform float Exposure;
uniform float Highlights;
uniform float Shadows;
uniform float Brightness;
uniform float Contrast;
uniform float BlackPoint;
uniform float Saturation;
uniform float Vibrance;
uniform float Cast;
uniform float Color_Gain_R;
uniform float Color_Gain_G;
uniform float Color_Gain_B;
vec3 apply_adjustments(vec3 color) {
    color.r *= Color_Gain_R;
    color.g *= Color_Gain_G;
    color.b *= Color_Gain_B;
    color *= pow(2.0, Exposure);
    float brilliance_val = Brilliance;
    if (brilliance_val > 0.0) {
        color = color * (1.0 - brilliance_val) + (color * color) * brilliance_val;
    } else {
        color = color * (1.0 + brilliance_val) - (color * color) * brilliance_val;
    }
    float highlights_val = Highlights;
    float shadows_val = Shadows;
    float luma = dot(color, vec3(0.2126, 0.7152, 0.0722));
    float shadow_factor = 1.0 - smoothstep(0.0, 0.4, luma);
    float highlight_factor = smoothstep(0.6, 1.0, luma);
    color += shadow_factor * shadows_val;
    color -= highlight_factor * highlights_val;
    color += Brightness;
    color = (color - 0.5) * (1.0 + Contrast) + 0.5;
    color = max(color - BlackPoint, 0.0);
    vec3 grayscale = vec3(dot(color, vec3(0.299, 0.587, 0.114)));
    color = mix(grayscale, color, 1.0 + Saturation);
    float vibrance_val = Vibrance;
    float max_color = max(color.r, max(color.g, color.b));
    float min_color = min(color.r, min(color.g, color.b));
    float sat = max_color - min_color;
    color = mix(grayscale, color, 1.0 + vibrance_val * (1.0 - sat));
    return clamp(color, 0.0, 1.0);
}
void main()
{
    vec4 texColor = texture(ourTexture, TexCoord);
    if (is_placeholder) {
        FragColor = texColor;
    } else {
        vec3 color = apply_adjustments(texColor.rgb);
        FragColor = vec4(color, texColor.a);
    }
}
"""
