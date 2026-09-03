
function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== "") {
        const cookies = document.cookie.split(";");
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            // Does this cookie string begin with the name we want?
            if (cookie.substring(0, name.length + 1) === name + "=") {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

function reloadEmployeeListContainer() {
    var applyBtn = document.getElementById("applyFilter");
    if (applyBtn && window.htmx) {
        applyBtn.click();
        if (typeof jQuery !== "undefined" && $("#reloadMessagesButton").length) {
            $("#reloadMessagesButton").click();
        }
        return;
    }
    var form = document.getElementById("filterForm");
    if (form && window.htmx) {
        htmx.trigger(form, "submit");
        if (typeof jQuery !== "undefined" && $("#reloadMessagesButton").length) {
            $("#reloadMessagesButton").click();
        }
        return;
    }
    window.location.reload();
}


var form = document.getElementById("workInfoImportForm");

// Add an event listener to the form submission
form.addEventListener("submit", function (event) {
    // Prevent the default form submission
    event.preventDefault();

    // Create a new form data object
    $(".oh-dropdown__import-form").css("display", "none");
    $("#uploading").css("display", "block");
    var formData = new FormData();

    // Append the file to the form data object
    var fileInput = document.querySelector("#workInfoImportFile");
    formData.append("file", fileInput.files[0]);
    $.ajax({
        type: "POST",
        url: "/employee/work-info-import/",
        dataType: "binary",
        data: formData,
        processData: false,
        contentType: false,
        headers: {
            "X-CSRFToken": getCookie("csrftoken"),
        },
        xhrFields: {
            responseType: "blob",
        },
        success: function (response, textStatus, xhr) {
            var errorCount = xhr.getResponseHeader('X-Error-Count');
            if (typeof response === 'object' && response.type == 'application/json') {
                var reader = new FileReader();

                reader.onload = function () {
                    var json = JSON.parse(reader.result);

                    if (json.success_count > 0) {
                        Swal.fire({
                            text: `${json.success_count} Employees Imported Successfully`,
                            icon: "success",
                            showConfirmButton: false,
                            timer: 3000,
                            timerProgressBar: true,
                        }).then(function () {
                            window.location.reload();
                        });
                    }
                }
                reader.readAsText(response);
                return;
            }
            if (!$(".file-xlsx-validation").length) {
                swal.fire({
                    text: `You have ${errorCount} errors. Do you want to download the error list?`,
                    icon: "error",
                    showCancelButton: true,
                    showDenyButton: true,
                    confirmButtonText: "Download error list & Skip Import",
                    denyButtonText: "Downlod error list & Continue Import",
                    cancelButtonText: i18nMessages.cancel,
                    confirmButtonColor: "#008000",
                    denyButtonColor: "#6c757d",
                    customClass: {
                        container: 'custom-swal-container'
                    }
                })
                    .then((result) => {
                        if (result.isConfirmed) {
                            const file = new Blob([response], {
                                type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            });
                            const url = URL.createObjectURL(file);
                            const link = document.createElement("a");
                            link.href = url;
                            link.download = "ImportError.xlsx";
                            document.body.appendChild(link);
                            link.click();
                            window.location.reload();
                        }
                        else if (result.isDenied) {
                            formData.append("create_work_info", true);
                            $.ajax({
                                type: "POST",
                                url: "/employee/work-info-import/",
                                dataType: "binary",
                                data: formData,
                                processData: false,
                                contentType: false,
                                headers: {
                                    "X-CSRFToken": getCookie("csrftoken"),
                                },
                                xhrFields: {
                                    responseType: "blob",
                                },
                                success: function (response, textStatus, xhr) {
                                    Swal.fire({
                                        text: `Employees Imported Successfully`,
                                        icon: "success",
                                        showConfirmButton: false,
                                        timer: 3000,
                                        timerProgressBar: true,
                                    }).then(function () {
                                        const file = new Blob([response], {
                                            type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                        });
                                        const url = URL.createObjectURL(file);
                                        const link = document.createElement("a");
                                        link.href = url;
                                        link.download = "ImportError.xlsx";
                                        document.body.appendChild(link);
                                        link.click();
                                        window.location.reload();
                                    });

                                    return;
                                }
                            })
                        }
                        else {
                            $(".oh-dropdown__import-form").css("display", "block");
                            $("#uploading").css("display", "none");
                        }
                    });
            }

        },
        error: function (xhr, textStatus, errorThrown) {
            console.error("Error downloading file:", errorThrown);
        },
    });
});



$(document).on("click", "#work-info-import", function (e) {
    e.preventDefault();

    Swal.fire({
        text: i18nMessages.downloadTemplate,
        icon: "question",
        showCancelButton: true,
        confirmButtonColor: "#008000",
        cancelButtonColor: "#6c757d",
        confirmButtonText: i18nMessages.confirm,
        cancelButtonText: i18nMessages.cancel,
    }).then(function (result) {
        if (result.isConfirmed) {
            $("#loading").show();

            var xhr = new XMLHttpRequest();
            xhr.open("GET", "/employee/work-info-import", true);
            xhr.responseType = "arraybuffer";

            xhr.upload.onprogress = function (e) {
                if (e.lengthComputable) {
                    var percent = (e.loaded / e.total) * 100;
                    $(".progress-bar")
                        .width(percent + "%")
                        .attr("aria-valuenow", percent);
                    $("#progress-text").text(
                        i18nMessages.uploading + percent.toFixed(2) + "%"
                    );
                }
            };

            xhr.onload = function (e) {
                if (this.status == 200) {
                    const file = new Blob([this.response], {
                        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    });
                    const url = URL.createObjectURL(file);
                    const link = document.createElement("a");
                    link.href = url;
                    link.download = "work_info_template.xlsx";
                    document.body.appendChild(link);
                    link.click();
                }
            };


            xhr.onerror = function () {
                console.error("Error downloading file:", xhr.statusText);
            };

            xhr.onerror = function (e) {
                console.error("Error downloading file:", e);
            };
            xhr.send();
        }
    });

});
$(document).ajaxStart(function () {
    $("#loading").show();
});

$(document).ajaxStop(function () {
    $("#loading").hide();
});

function simulateProgress() {

    let progressBar = document.querySelector(".progress-bar");
    let progressText = document.getElementById("progress-text");

    let width = 0;
    let interval = setInterval(function () {
        if (width >= 100) {
            clearInterval(interval);
            progressText.innerText = uploadMessage;
            setTimeout(function () {
                document.getElementById("loading").style.display = "none";
            }, 3000);
            Swal.fire({
                text: importMessage,
                icon: "success",
                showConfirmButton: false,
                timer: 2000,
                timerProgressBar: true,
            });
            setTimeout(function () {
                $("#workInfoImport").removeClass("oh-modal--show");
                location.reload(true);
            }, 2000);
        } else {
            width++;
            progressBar.style.width = width + "%";
            progressBar.setAttribute("aria-valuenow", width);
            progressText.innerText = i18nMessages.uploading + width + "%";
        }
    }, 20);

}



$(document).on("click", "#archiveEmployees", function (e) {
    e.preventDefault();
    ids = [];
    ids.push($("#selectedInstances").attr("data-ids"));
    ids = JSON.parse($("#selectedInstances").attr("data-ids"));
    if (ids.length === 0) {
        Swal.fire({
            text: i18nMessages.noRowsSelected,
            icon: "warning",
            confirmButtonText: i18nMessages.close,
        });
    } else {
        Swal.fire({
            text: i18nMessages.confirmBulkArchive,
            icon: "info",
            showCancelButton: true,
            confirmButtonColor: "#d33",
            cancelButtonColor: "#6c757d",
            confirmButtonText: i18nMessages.confirm,
            cancelButtonText: i18nMessages.cancel,
        }).then(function (result) {
            if (result.isConfirmed) {
                e.preventDefault();
                ids = [];
                ids.push($("#selectedInstances").attr("data-ids"));
                ids = JSON.parse($("#selectedInstances").attr("data-ids"));
                $.ajax({
                    type: "POST",
                    url: "/employee/employee-bulk-archive/?is_active=False",
                    data: {
                        csrfmiddlewaretoken: getCookie("csrftoken"),
                        ids: JSON.stringify(ids),
                    },
                    success: function (response, textStatus, jqXHR) {
                        if (jqXHR.status === 200) {
                            reloadEmployeeListContainer();
                        } else {
                            // console.log("Unexpected HTTP status:", jqXHR.status);
                        }
                    },
                });
            }
        });
    }
});


$(document).on("click", "#unArchiveEmployees", function (e) {
    e.preventDefault();

    ids = [];
    ids.push($("#selectedInstances").attr("data-ids"));
    ids = JSON.parse($("#selectedInstances").attr("data-ids"));
    if (ids.length === 0) {
        Swal.fire({
            text: i18nMessages.noRowsSelected,
            icon: "warning",
            confirmButtonText: i18nMessages.close,
        });
    } else {
        Swal.fire({
            text: i18nMessages.confirmBulkUnArchive,
            icon: "info",
            showCancelButton: true,
            confirmButtonColor: "#008000",
            cancelButtonColor: "#6c757d",
            confirmButtonText: i18nMessages.confirm,
            cancelButtonText: i18nMessages.cancel,
        }).then(function (result) {
            if (result.isConfirmed) {
                e.preventDefault();

                ids = [];

                ids.push($("#selectedInstances").attr("data-ids"));
                ids = JSON.parse($("#selectedInstances").attr("data-ids"));

                $.ajax({
                    type: "POST",
                    url: "/employee/employee-bulk-archive/?is_active=True",
                    data: {
                        csrfmiddlewaretoken: getCookie("csrftoken"),
                        ids: JSON.stringify(ids),
                    },
                    success: function (response, textStatus, jqXHR) {
                        if (jqXHR.status === 200) {
                            reloadEmployeeListContainer();
                        } else {
                            // console.log("Unexpected HTTP status:", jqXHR.status);
                        }
                    },
                });
            }
        });
    }
});

$(document).on("click", "#employeeBulkUpdateId", function (e) {
    ids = [];
    ids.push($("#selectedInstances").attr("data-ids"));
    ids = JSON.parse($("#selectedInstances").attr("data-ids"));
    if (ids.length === 0) {
        $("#bulkUpdateModal").removeClass("oh-modal--show");
        Swal.fire({
            text: i18nMessages.noRowsSelected,
            icon: "warning",
            confirmButtonText: i18nMessages.close,
        });
    } else {
        $("#id_bulk_employee_ids").val(JSON.stringify(ids));
        $("#bulkUpdateModal").addClass("oh-modal--show");
    }
});


/**
 * Phase EMPLOYEE-BULK-DELETE-2 — two-stage permanent account deletion.
 *
 * The previous handler asked one yes/no question and posted straight to the
 * delete endpoint, which answered "Success" whether or not anything was
 * actually removed — and for any employee with attendance history, nothing
 * was. The first request now only *previews*: the backend reports what each
 * selected employee owns, and that summary is what the operator reads before
 * typing the confirmation phrase back.
 *
 * None of this is trusted by the server. The counts are recomputed and the
 * phrase is rebuilt from the validated selection on the delete call; this
 * code exists to make the consequences visible, not to enforce them.
 */
function selectedEmployeeIdsForDelete() {
    try {
        return JSON.parse($("#selectedInstances").attr("data-ids") || "[]");
    } catch (err) {
        return [];
    }
}

function bulkDeleteErrorHtml(errors) {
    if (!errors || !errors.length) {
        return "<p>Không thực hiện được thao tác xóa.</p>";
    }
    var items = errors.map(function (item) {
        var who = item.name
            ? item.name
            : item.employee_id
            ? "Mã " + item.employee_id
            : "";
        return "<li>" + (who ? "<strong>" + who + ":</strong> " : "") +
            (item.message || item.code) + "</li>";
    });
    return "<p>Không có tài khoản nào bị xóa.</p><ul style='text-align:left'>" +
        items.join("") + "</ul>";
}

function bulkDeletePreviewHtml(data) {
    var parts = ["<p>Bạn đã chọn <strong>" + data.selected_count +
        "</strong> nhân viên.</p>"];

    var withHistory = data.employees.filter(function (row) {
        return row.total_owned_records > 0;
    });
    if (withHistory.length) {
        parts.push("<div style='text-align:left;max-height:220px;overflow:auto'>");
        withHistory.forEach(function (row) {
            var lines = [];
            if (row.attendance_count) {
                lines.push("<li>" + row.attendance_count + " bản ghi chấm công</li>");
            }
            if (row.request_count) {
                lines.push("<li>" + row.request_count + " đơn từ</li>");
            }
            if (row.document_count) {
                lines.push("<li>" + row.document_count + " chứng từ</li>");
            }
            if (row.other_owned_records) {
                lines.push("<li>" + row.other_owned_records + " bản ghi khác</li>");
            }
            parts.push("<p style='margin:6px 0 2px'><strong>" + row.name +
                "</strong></p><ul style='margin:0'>" + lines.join("") + "</ul>");
        });
        parts.push("</div>");
    }

    parts.push("<p style='text-align:left;margin-top:10px'>Toàn bộ tài khoản " +
        "đăng nhập và dữ liệu thuộc về các nhân viên đã chọn sẽ bị " +
        "<strong>xóa vĩnh viễn</strong>.<br>Dữ liệu công ty, phòng ban, ca làm " +
        "việc và cấu hình dùng chung sẽ không bị xóa.</p>");

    parts.push("<p style='text-align:left;margin-top:10px'>Để xác nhận, gõ " +
        "chính xác:<br><code>" + data.confirmation_phrase + "</code></p>");
    parts.push("<input id='bulkDeleteConfirmInput' class='swal2-input' " +
        "autocomplete='off' placeholder='" + data.confirmation_phrase + "'>");

    return parts.join("");
}

function runBulkEmployeeDelete(ids, phrase) {
    $.ajax({
        type: "POST",
        url: "/employee/employee-bulk-delete/",
        data: {
            csrfmiddlewaretoken: getCookie("csrftoken"),
            action: "delete",
            ids: JSON.stringify(ids),
            confirmation: phrase,
        },
        success: function (response) {
            // The backend is the only authority on what happened: it reports
            // success only when every selected account was deleted inside one
            // transaction.
            if (response && response.success) {
                
                reloadEmployeeListContainer();
            } else {
                Swal.fire({
                    title: "Xóa tài khoản thất bại",
                    html: bulkDeleteErrorHtml(response && response.errors),
                    icon: "error",
                    confirmButtonText: i18nMessages.close,
                });
            }
        },
        error: function (jqXHR) {
            var payload = jqXHR.responseJSON;
            Swal.fire({
                title: "Xóa tài khoản thất bại",
                html: bulkDeleteErrorHtml(payload && payload.errors),
                icon: "error",
                confirmButtonText: i18nMessages.close,
            });
        },
    });
}

$(document).on("click", "#deleteEmployees", function (e) {
    e.preventDefault();
    var ids = selectedEmployeeIdsForDelete();
    if (ids.length === 0) {
        Swal.fire({
            text: i18nMessages.noRowsSelected,
            icon: "warning",
            confirmButtonText: i18nMessages.close,
        });
        return;
    }

    // Stage 1 — ask the server what deleting this selection would destroy.
    // This request changes nothing.
    $.ajax({
        type: "POST",
        url: "/employee/employee-bulk-delete/",
        data: {
            csrfmiddlewaretoken: getCookie("csrftoken"),
            action: "preview",
            ids: JSON.stringify(ids),
        },
        success: function (data) {
            if (!data || !data.success) {
                Swal.fire({
                    title: "Không thể xóa",
                    html: bulkDeleteErrorHtml(data && data.errors),
                    icon: "error",
                    confirmButtonText: i18nMessages.close,
                });
                return;
            }

            // Stage 2 — the destructive button stays disabled until the
            // phrase matches exactly, so the confirmation cannot be dismissed
            // by reflex.
            Swal.fire({
                title: "Xóa tài khoản nhân viên?",
                html: bulkDeletePreviewHtml(data),
                icon: "warning",
                showCancelButton: true,
                confirmButtonColor: "#d33",
                cancelButtonColor: "#6c757d",
                confirmButtonText: "Xóa vĩnh viễn",
                cancelButtonText: i18nMessages.cancel,
                focusConfirm: false,
                didOpen: function () {
                    var button = Swal.getConfirmButton();
                    var input = document.getElementById(
                        "bulkDeleteConfirmInput"
                    );
                    button.disabled = true;
                    input.addEventListener("input", function () {
                        button.disabled =
                            input.value.trim() !== data.confirmation_phrase;
                    });
                    input.focus();
                },
                preConfirm: function () {
                    var input = document.getElementById(
                        "bulkDeleteConfirmInput"
                    );
                    return input ? input.value.trim() : "";
                },
            }).then(function (result) {
                if (result.isConfirmed && result.value === data.confirmation_phrase) {
                    // Re-read the selection: the ids sent are the ids the
                    // count in the dialog was computed from.
                    runBulkEmployeeDelete(ids, data.confirmation_phrase);
                }
            });
        },
        error: function (jqXHR) {
            var payload = jqXHR.responseJSON;
            Swal.fire({
                title: "Không thể xóa",
                html: bulkDeleteErrorHtml(payload && payload.errors),
                icon: "error",
                confirmButtonText: i18nMessages.close,
            });
        },
    });
});
