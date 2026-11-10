#include "4DPlugin-JSON.h"
#include "ARRAY_TEXT.h"
#include "PrivateTypes.h"
#include "EntryPoints.h"
#include <cassert>
#include <cstdlib>
#include <cstring>

// Only the engine boundary is simulated; all PA_* and helper code is compiled unchanged.
struct Object {
    size_t refs = 1;
    std::map<CUTF16String, PA_Variable> properties;
};

static std::map<PA_Unichar*, size_t> strings;
static std::map<PA_Handle, size_t> handles;
static std::map<Object*, size_t> objects;
static bool nullEmpty;
static bool stringifyMismatch;

static PA_Unistring makeString(const CUTF16String& text) {
    PA_Unistring result = {};
    result.fLength = static_cast<PA_long32>(text.size());
    if (!text.empty() || !nullEmpty) {
        result.fString = new PA_Unichar[text.size() + 1];
        std::copy(text.begin(), text.end(), result.fString);
        result.fString[text.size()] = 0;
        PA_Unichar* buffer = result.fString;
        strings.emplace(buffer, text.size());
    }
    return result;
}

static CUTF16String readString(PA_Unistring text) {
    PA_Unichar* buffer = text.fString;
    if (!buffer) {
        assert(text.fLength == 0);
        return {};
    }
    assert(strings.count(buffer));
    assert(strings.at(buffer) == static_cast<size_t>(text.fLength));
    assert(buffer[text.fLength] == 0);
    return CUTF16String(buffer, text.fLength);
}

static void freeString(PA_Unistring text) {
    PA_Unichar* buffer = text.fString;
    if (buffer) {
        assert(strings.erase(buffer) == 1);
        delete[] buffer;
    }
}

static void clear(PA_Variable& value);

static Object* makeObject() {
    auto object = new Object;
    objects.emplace(object, 1);
    return object;
}

static void release(Object* object) {
    if (!object) return;
    assert(objects.count(object));
    if (--object->refs == 0) {
        for (auto& property : object->properties) clear(property.second);
        objects.erase(object);
        delete object;
    }
}

static PA_Variable copy(PA_Variable value) {
    if (value.fType == eVK_Unistring) {
        value.uValue.fString = makeString(readString(value.uValue.fString));
    } else if (value.fType == eVK_Object || value.fType == eVK_Collection) {
        auto object = static_cast<Object*>(value.uValue.fObject);
        if (object) {
            assert(objects.count(object));
            ++object->refs;
        }
    }
    return value;
}

static void clear(PA_Variable& value) {
    if (value.fType == eVK_Unistring) {
        freeString(value.uValue.fString);
    } else if (value.fType == eVK_Object || value.fType == eVK_Collection) {
        release(static_cast<Object*>(value.uValue.fObject));
    } else if (value.fType == eVK_ArrayUnicode && value.uValue.fArray.fData) {
        auto handle = value.uValue.fArray.fData;
        assert(handles.count(handle));
        auto elements = reinterpret_cast<PA_Unistring*>(*handle);
        for (PA_long32 i = 0; i <= value.uValue.fArray.fNbElements; ++i)
            freeString(elements[i]);
        std::free(*handle);
        delete handle;
        handles.erase(handle);
    }
    value = {};
    value.fType = eVK_Undefined;
}

static void engine(short selector, EngineBlock* eb) {
    eb->fError = eER_NoErr;
    switch (selector) {
        case EX_CREATE_UNISTRING: {
            auto input = reinterpret_cast<PA_Unichar*>(eb->fHandle);
            eb->fUniString1 = makeString(CUTF16String(input));
            break;
        }
        case EX_DISPOSE_UNISTRING:
            freeString(eb->fUniString1);
            break;
        case EX_VARIABLE_TO_STRING: {
            auto value = reinterpret_cast<PA_Variable*>(eb->fHandle);
            assert(value->fType == eVK_Unistring);
            readString(value->uValue.fString);
            eb->fUniString1 = value->uValue.fString;
            break;
        }
        case EX_CLEAR_VARIABLE:
            clear(*static_cast<PA_Variable*>(eb->fPtr1));
            break;
        case EX_GET_OBJ_VALUE:
        case EX_SET_OBJ_VALUE: {
            assert(eb->fShort == eVK_Object);
            auto object = static_cast<Object*>(eb->fPtr1);
            assert(objects.count(object));
            auto key = readString(eb->fUniString1);
            auto value = static_cast<PA_Variable*>(eb->fPtr2);
            if (selector == EX_GET_OBJ_VALUE) {
                *value = copy(object->properties.at(key));
            } else {
                auto incoming = copy(*value);
                auto found = object->properties.find(key);
                if (found != object->properties.end()) clear(found->second);
                object->properties[key] = incoming;
            }
            break;
        }
        case EX_HANDLE_MANAGER: {
            auto handle = eb->fHandle;
            sLONG_PTR size = eb->fParam2;
            switch (eb->fParam1) {
                case 1:
                    handle = new char*;
                    *handle = static_cast<char*>(std::calloc(1, std::max<sLONG_PTR>(1, size)));
                    assert(*handle);
                    handles.emplace(handle, size);
                    eb->fHandle = handle;
                    break;
                case 2:
                    assert(handles.erase(handle) == 1);
                    std::free(*handle);
                    delete handle;
                    break;
                case 3:
                    assert(handles.count(handle));
                    *handle = static_cast<char*>(std::realloc(*handle, eb->fParam2));
                    assert(*handle);
                    handles[handle] = eb->fParam2;
                    break;
                case 4:
                    eb->fParam2 = handles.at(handle);
                    break;
                case 5:
                    assert(handles.count(handle));
                    eb->fParam3 = reinterpret_cast<sLONG_PTR>(*handle);
                    break;
                case 6:
                    assert(handles.count(handle));
                    break;
                default: assert(false);
            }
            break;
        }
        case EX_EXECUTE_COMMAND_BY_ID: {
            auto args = static_cast<PA_Variable**>(eb->fPtr1);
            auto result = static_cast<PA_Variable*>(eb->fPtr2);
            if (eb->fParam1 == 1231) {
                assert(eb->fParam2 == 2);
                auto object = static_cast<Object*>(args[0]->uValue.fObject);
                PA_SetBooleanVariable(result,
                    object->properties.count(readString(args[1]->uValue.fString)) != 0);
            } else if (eb->fParam1 == 1225) {
                assert(eb->fParam2 == 1);
                auto original = static_cast<Object*>(args[0]->uValue.fObject);
                auto duplicate = makeObject();
                for (auto& property : original->properties)
                    duplicate->properties.emplace(property.first, copy(property.second));
                PA_SetObjectVariable(result, duplicate);
            } else if (eb->fParam1 == 1217) {
                assert(eb->fParam2 == 1);
                assert(objects.count(static_cast<Object*>(args[0]->uValue.fObject)));
                if (stringifyMismatch) {
                    PA_SetObjectVariable(result, makeObject());
                } else {
                    auto text = makeString(CUTF16String{ '{', '}', 0x00e9, 0xd83d, 0xde00 });
                    PA_SetStringVariable(result, &text);
                }
            } else {
                assert(false);
            }
            break;
        }
        default: assert(false);
    }
}

void PluginMain(PA_long32, PA_PluginParameters) {}

struct Fixture {
    const char* utf8;
    const wchar_t* wide;
    CUTF16String utf16;
};

static const Fixture fixtures[] = {
    { "ASCII", L"ASCII", {'A', 'S', 'C', 'I', 'I'} },
    { "\xc3\xa9\xe6\x97\xa5", L"\u00e9\u65e5", {0x00e9, 0x65e5} },
    { "\xf0\x9f\x98\x80", L"\U0001f600", {0xd83d, 0xde00} },
    { "", L"", {} },
};

static CUTF8String utf8(const char* text) {
    return CUTF8String(reinterpret_cast<const uint8_t*>(text));
}

static void assertEmpty() {
    if (!strings.empty()) std::cerr << "unreleased SDK string allocations: " << strings.size() << "\n";
    assert(strings.empty());
    assert(handles.empty());
    assert(objects.empty());
}

static void setters(int overload) {
    auto object = makeObject();
    for (const auto& fixture : fixtures) {
        switch (overload) {
            case 0: ob_set_s(object, "key", fixture.utf8); break;
            case 1: ob_set_s(object, L"key", fixture.utf8); break;
            case 2: ob_set_a(object, L"key", fixture.wide); break;
            case 3: ob_set_a(object, L"key", &fixture.utf16); break;
            default: assert(false);
        }
        auto value = object->properties.at(CUTF16String{'k', 'e', 'y'});
        assert(readString(value.uValue.fString) == fixture.utf16);
        CUTF16String output = readString(value.uValue.fString);
        for (int i = 0; i != 20; ++i) {
            auto temporary = makeString(fixture.utf16);
            freeString(temporary);
        }
        assert(output == fixture.utf16);
    }
    PA_DisposeObject(object);
    assertEmpty();
}

static void getters(bool mismatch, int getter) {
    for (const auto& fixture : fixtures) {
        auto object = makeObject();
        PA_Variable property = {};
        auto key = CUTF16String{'k', 'e', 'y'};
        if (mismatch && getter == 0) {
            PA_SetObjectVariable(&property, makeObject());
        } else if (mismatch || getter == 0) {
            auto text = makeString(fixture.utf16);
            PA_SetStringVariable(&property, &text);
        } else if (getter == 1) {
            PA_SetBooleanVariable(&property, true);
        } else if (getter == 2) {
            PA_SetRealVariable(&property, 42.5);
        } else if (getter == 3) {
            PA_SetObjectVariable(&property, makeObject());
        } else {
            PA_SetCollectionVariable(&property, makeObject());
        }
        object->properties.emplace(key, property);
        size_t before = strings.size();
        switch (getter) {
            case 0: {
                CUTF8String output = utf8("unchanged");
                assert(ob_get_a(object, L"key", &output));
                for (int i = 0; i != 20; ++i) {
                    auto temporary = makeString(fixture.utf16);
                    freeString(temporary);
                }
                assert(output == utf8(mismatch ? "unchanged" : fixture.utf8));
                break;
            }
            case 1: assert(ob_get_b(object, L"key") == !mismatch); break;
            case 2: assert(ob_get_n(object, L"key") == (mismatch ? 0 : 42.5)); break;
            case 3: {
                auto output = static_cast<Object*>(ob_get_o(object, L"key"));
                if (mismatch) assert(!output);
                else {
                    assert(output == property.uValue.fObject && output->refs == 2);
                    PA_DisposeObject(output);
                }
                break;
            }
            case 4: {
                auto output = static_cast<Object*>(ob_get_c(object, L"key"));
                if (mismatch) assert(!output);
                else {
                    assert(output == property.uValue.fCollection && output->refs == 2);
                    PA_DisposeCollection(output);
                }
                break;
            }
            default: assert(false);
        }
        assert(strings.size() == before);
        CUTF8String absent = utf8("unchanged");
        assert(!ob_get_a(object, L"absent", &absent));
        assert(absent == utf8("unchanged"));
        PA_DisposeObject(object);
        assertEmpty();
    }
}

static void arrays() {
    PA_Variable array = PA_CreateVariable(eVK_ArrayUnicode);
    {
        ARRAY_TEXT source;
        for (const auto& fixture : fixtures)
            source.appendUTF16String(fixture.utf16.c_str(), fixture.utf16.size());
        uint8_t* params[] = {reinterpret_cast<uint8_t*>(&array)};
        source.toParamAtIndex(params, 1);
        ARRAY_TEXT copied;
        copied.fromParamAtIndex(params, 1);
        source.toParamAtIndex(params, 1); // replacement must not invalidate copied input
        PA_ResizeArray(&array, 0);
        for (size_t i = 0; i != 4; ++i) {
            CUTF16String text;
            copied.copyUTF16StringAtIndex(&text, i);
            assert(text == fixtures[i].utf16);
        }
    }
    for (int rejection = 0; rejection != 4; ++rejection) {
        auto text = makeString(fixtures[0].utf16);
        auto destination = array;
        PA_long32 index = 0;
        if (rejection == 0) destination.fType = eVK_Longint;
        if (rejection == 1) index = -1;
        if (rejection == 2) index = 1;
        if (rejection == 3) destination.uValue.fArray.fData = nullptr;
        PA_SetStringInArray(destination, index, &text);
        assert(readString(text) == fixtures[0].utf16);
        PA_DisposeUnistring(&text);
    }
    PA_SetStringInArray(array, 0, nullptr);
    PA_ClearVariable(&array);
    assertEmpty();
}

static void stringify() {
    for (bool mismatch : {false, true}) {
        auto object = makeObject();
        stringifyMismatch = mismatch;
        CUTF8String output = utf8("unchanged");
        ob_stringify(object, &output);
        assert(output == utf8(mismatch ? "unchanged" : "{}\xc3\xa9\xf0\x9f\x98\x80"));
        assert(object->refs == 1);
        PA_DisposeObject(object);
        assertEmpty();
    }
}

int main(int argc, char** argv) {
    assert(argc == 3);
    gCall4D = engine;
    nullEmpty = std::string(argv[2]) == "null";
    std::string test = argv[1];
    if (test.compare(0, 6, "setter") == 0) setters(std::stoi(test.substr(6)));
    else if (test.compare(0, 6, "getter") == 0) getters(false, std::stoi(test.substr(6)));
    else if (test.compare(0, 8, "mismatch") == 0) getters(true, std::stoi(test.substr(8)));
    else if (test == "arrays") arrays();
    else if (test == "stringify") stringify();
    else assert(false);
    assertEmpty();
    std::cout << test << " " << argv[2] << ": PASS\n";
}
