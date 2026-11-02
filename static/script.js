
// The Flask backend now handles application submissions.
// Additional frontend interactions can be added here.

console.log("FUDMA Hostel Allocation System loaded.");

const registrationForm = document.getElementById("applicationForm");

    registrationForm.addEventListener("submit", function (event) {
        const password = document.getElementById("password").value;
        const confirmPassword =
            document.getElementById("confirm_password").value;

        if (password !== confirmPassword) {
            event.preventDefault();
            alert("The passwords do not match. Please check and try again.");
        }
    });
